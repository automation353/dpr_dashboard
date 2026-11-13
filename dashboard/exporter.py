"""Background Excel export of the All Details rows.

Why a background job: a long range (e.g. April -> today, ~590k lines) takes
minutes to write. Doing that inside a web request ties up a gunicorn worker
and risks timeouts. Instead:

  start_job(from, to)  -> writes exports/<id>.json and launches
                          `manage.py export_dpr <id>` as a separate, low-priority
                          process with a memory cap; returns the job id.
  run_job(id)          -> (in that process) streams the API page by page into a
                          write-only workbook, updating progress in <id>.json.
  job_status(id)       -> progress for the page to poll.

Server protection (this box has 2 CPU cores shared with other apps):
  * the export runs as its own systemd unit with CPUQuota=50% (at most half of
    one core), MemoryMax=1G and Nice=10, so it can never take over the server;
  * it pauses between API pages so the dpr-api / PostgreSQL side stays light;
  * any date range is allowed; ranges with more rows than one Excel sheet holds
    continue on extra sheets (All details (2), (3), ...);
  * only ONE export runs at a time (an exclusive lock file); a second request
    gets "busy" with the running job's progress. Files older than a day are deleted.
"""

import fcntl
import json
import logging
import os
import re
import resource
import shutil
import subprocess
import sys
import time
import uuid
from datetime import date
from pathlib import Path

from django.conf import settings
import queue
import threading


from .sheet_reader import _iter_api_rows, count_dpr_lines
from .xlsx_stream import XlsxStreamWriter

log = logging.getLogger(__name__)

EXPORT_DIR = Path(getattr(settings, 'DPR_EXPORT_DIR', settings.BASE_DIR / 'exports'))
LOCK_FILE = EXPORT_DIR / '.export.lock'
SHEET_MAX_ROWS = 1_000_000          # data rows per sheet (Excel's limit is 1,048,575)
KEEP_SECONDS = 24 * 3600            # finished files are kept for a day
MEMORY_CAP = 1536 * 1024 * 1024     # address-space cap (fallback when systemd-run is missing)
CPU_QUOTA = '50%'                   # of ONE core, for the whole export unit
MEMORY_MAX = '1G'
PAGE_PAUSE = 0.2                    # seconds to rest between API pages
# Internal database columns: no business meaning, and row_hash/fetched_at are
# near-random text that barely compresses (they were ~25% of a 36 MB file).
EXPORT_SKIP_COLUMNS = {'id', 'row_hash', 'fetched_at'}
_JOB_ID = re.compile(r'^[0-9a-f]{32}$')

_PLAIN_NUMBER = re.compile(r'^-?(0|[1-9]\d*)(\.\d+)?$')   # no leading zeros, no exponent
_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _xl_value(v):
    """Detail cell (string) -> Excel value: real numbers/dates, codes stay text."""
    t = str(v)
    if _PLAIN_NUMBER.match(t):
        return float(t) if '.' in t else int(t)
    if _ISO_DATE.match(t):
        try:
            return date.fromisoformat(t)
        except ValueError:
            return t
    return t


# ---------------------------------------------------------------- job files
def valid_id(job_id):
    return bool(_JOB_ID.match(str(job_id or '')))


def _meta_path(job_id):
    return EXPORT_DIR / f'{job_id}.json'


def file_path(job_id):
    return EXPORT_DIR / f'{job_id}.xlsx'


def _part_path(job_id):
    return EXPORT_DIR / f'{job_id}.xlsx.part'


def _cancel_path(job_id):
    # a separate flag file, so the job's own progress writes can't overwrite it
    return EXPORT_DIR / f'{job_id}.cancel'


class _Cancelled(Exception):
    pass


def _write_meta(job_id, **fields):
    p = _meta_path(job_id)
    meta = json.loads(p.read_text()) if p.exists() else {}
    meta.update(fields, updated=time.time())
    tmp = p.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(meta))
    tmp.replace(p)                                   # atomic: readers never see half a file
    return meta


def job_status(job_id):
    if not valid_id(job_id) or not _meta_path(job_id).exists():
        return None
    meta = json.loads(_meta_path(job_id).read_text())
    # a "running" job whose process died (no lock held) is reported as failed
    if meta.get('state') in ('queued', 'running') and not _lock_held():
        if time.time() - meta.get('updated', 0) > 30:
            meta = _write_meta(job_id, state='error',
                               error='export process stopped unexpectedly')
    return meta


def _lock_held():
    """True if some export process currently holds the export lock."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, 'a+') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def _running_job():
    for p in sorted(EXPORT_DIR.glob('*.json'), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            meta = json.loads(p.read_text())
        except ValueError:
            continue
        if meta.get('state') in ('queued', 'running'):
            return meta
    return None


def cleanup_old():
    now = time.time()
    for p in EXPORT_DIR.glob('*'):
        if p.name.startswith('.'):
            continue
        try:
            if now - p.stat().st_mtime > KEEP_SECONDS:
                p.unlink()
        except FileNotFoundError:
            pass


def cancel_job(job_id):
    """Ask an export to stop. The running process checks about once a second,
    stops and deletes its partial file. Returns the job's meta (or None)."""
    meta = job_status(job_id)
    if not meta or meta.get('state') not in ('queued', 'running', 'saving'):
        return meta
    _cancel_path(job_id).touch()
    if not _lock_held():                     # not started yet: nothing to stop
        return _write_meta(job_id, state='cancelled', error=None)
    return {**meta, 'state': 'cancelling'}


# ---------------------------------------------------------------- start
def start_job(from_iso, to_iso):
    """Validate, then launch the export process. Returns (status_code, payload)."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old()
    # Serialise start requests, and count a just-queued job as busy too: its
    # process may not have taken the export lock yet.
    with open(EXPORT_DIR / '.start.lock', 'a+') as start_lock:
        fcntl.flock(start_lock, fcntl.LOCK_EX)
        return _start_locked(from_iso, to_iso)


def _start_locked(from_iso, to_iso):
    pending = _running_job()
    if pending and pending.get('state') == 'queued' and \
            time.time() - pending.get('updated', 0) > 60:
        pending = None                               # stale queued job that never started
    if _lock_held() or pending:
        running = pending or _running_job() or {}
        return 409, {'ok': False, 'busy': True,
                     'error': 'Another Excel export is being prepared. Please try again '
                              'in a moment.',
                     'running': {k: running.get(k) for k in ('from', 'to', 'rows', 'total')}}

    total = count_dpr_lines(from_iso, to_iso)
    if total == 0:
        return 400, {'ok': False, 'error': 'No rows in this date range.'}
    job_id = uuid.uuid4().hex
    name = f'DPR_all_details_{from_iso}_to_{to_iso}.xlsx'
    _write_meta(job_id, id=job_id, state='queued', **{'from': from_iso, 'to': to_iso},
                total=total, rows=0, name=name, created=time.time(), error=None)

    _launch(job_id)
    return 202, {'ok': True, 'id': job_id, 'total': total}


def _launch(job_id):
    """Start `manage.py export_dpr <id>` as a capped, low-priority systemd unit."""
    cmd = [sys.executable, str(settings.BASE_DIR / 'manage.py'), 'export_dpr', job_id]
    systemd_run = shutil.which('systemd-run')
    if systemd_run:
        res = subprocess.run(
            [systemd_run, '--quiet', '--collect', f'--unit=dpr-export-{job_id[:12]}',
             f'--working-directory={settings.BASE_DIR}',
             '-p', f'CPUQuota={CPU_QUOTA}', '-p', f'MemoryMax={MEMORY_MAX}',
             '-p', 'Nice=10', '-p', 'IOSchedulingClass=idle',
             '-p', f'StandardOutput=append:{EXPORT_DIR / "export.log"}',
             '-p', f'StandardError=append:{EXPORT_DIR / "export.log"}',
             *cmd],
            capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            return
        log.warning('systemd-run failed (%s); using a plain low-priority process',
                    (res.stderr or '').strip()[:200])

    def limits():
        os.nice(10)
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_CAP, MEMORY_CAP))

    with open(EXPORT_DIR / 'export.log', 'a') as log_fh:
        subprocess.Popen(cmd, cwd=str(settings.BASE_DIR), stdin=subprocess.DEVNULL,
                         stdout=log_fh, stderr=log_fh, start_new_session=True,
                         preexec_fn=limits, close_fds=True)


# ---------------------------------------------------------------- run (export process)
def run_job(job_id):
    meta = job_status(job_id)
    if not meta:
        raise SystemExit(f'unknown export job {job_id}')
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _write_meta(job_id, state='error', error='Another export is running; try again.')
            return
        try:
            if _cancel_path(job_id).exists():
                raise _Cancelled
            _write(job_id, meta)
        except _Cancelled:
            log.info('export %s cancelled', job_id)
            _write_meta(job_id, state='cancelled', error=None)
            _remove_partial(job_id)
        except Exception as exc:                    # noqa: BLE001
            log.exception('export %s failed', job_id)
            _write_meta(job_id, state='error', error=str(exc)[:500])
            _remove_partial(job_id)


def _remove_partial(job_id):
    for p in (_part_path(job_id), file_path(job_id)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


_DONE = object()


def _prefetch(stream, chunk=2000, depth=8):
    """Run the API stream in a background thread so the next page is being
    fetched (by dpr-api, in its own process) while this one is written."""
    q = queue.Queue(maxsize=depth)

    def pump():
        try:
            batch = []
            for item in stream:
                batch.append(item)
                if len(batch) >= chunk:
                    q.put(batch)
                    batch = []
            if batch:
                q.put(batch)
            q.put(_DONE)
        except BaseException as exc:                 # noqa: BLE001  - re-raised below
            q.put(exc)

    threading.Thread(target=pump, daemon=True, name='dpr-export-fetch').start()
    while True:
        item = q.get()
        if item is _DONE:
            return
        if isinstance(item, BaseException):
            raise item
        yield from item


def _write(job_id, meta):
    _write_meta(job_id, state='running', started=time.time())
    stream = _prefetch(_iter_api_rows(meta['from'], meta['to'], page_pause=PAGE_PAUSE))
    tmp = _part_path(job_id)
    cancel_flag = _cancel_path(job_id)
    n = 0
    with XlsxStreamWriter(str(tmp)) as xw:
        header = next(stream, None)
        if header is None:
            xw.start_sheet('All details', [])
        else:
            keep = [i for i, h in enumerate(header) if h not in EXPORT_SKIP_COLUMNS]
            header = [header[i] for i in keep]
            sheet_no, on_sheet = 1, 0
            xw.start_sheet('All details', header)
            for row in stream:
                if on_sheet >= SHEET_MAX_ROWS:          # sheet full: continue on the next one
                    sheet_no += 1
                    on_sheet = 0
                    xw.start_sheet(f'All details ({sheet_no})', header)
                xw.write_row([_xl_value(row[i]) for i in keep])
                n += 1
                on_sheet += 1
                if n % 5000 == 0:                       # ~once a second
                    if cancel_flag.exists():
                        raise _Cancelled
                    _write_meta(job_id, rows=n)
        if cancel_flag.exists():
            raise _Cancelled
        _write_meta(job_id, rows=n, state='saving')
    tmp.replace(file_path(job_id))
    _write_meta(job_id, rows=n, state='done', finished=time.time(),
                size=file_path(job_id).stat().st_size)
