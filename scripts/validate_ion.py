"""Validate the DPR dashboard against TCS iON, day by day.

Read-only: uses /opt/dpr_sync's fetch() to call iON directly (no DB writes),
then compares with the dashboard's /api/data (pivot) and /api/detail (lines).
Usage (from the repo root):
  python3 scripts/validate_ion.py 2026-11-05                 # one day
  python3 scripts/validate_ion.py 2026-10-01:2026-10-31      # a range
  python3 scripts/validate_ion.py 2026-10-01:2026-10-31 2026-11-05   # mix

Needs /opt/dpr_sync (its .env holds the iON keys; nothing iON-related is stored
in this repo). Set DPR_SYNC_DIR / DPR_DASHBOARD_URL to override the defaults.
Exit code is 0 when every day matches, 1 otherwise.
"""
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.environ.get('DPR_SYNC_DIR', '/opt/dpr_sync'))
import common  # noqa: E402  (loads iON config from /opt/dpr_sync/.env)
from apis import direct_jr  # noqa: E402

DASH = os.environ.get('DPR_DASHBOARD_URL', 'http://127.0.0.1:8020')


def dash(path):
    with urllib.request.urlopen(DASH + path, timeout=300) as r:
        return json.load(r)


def num(v):
    n = common.to_num(v)
    return float(n) if n is not None else 0.0


def summarise(lines, get):
    """lines -> (docs, line_count, {(jr, erp): produced}, per-JR produced)"""
    once = {}
    for r in lines:
        erp = (get(r, 'Finished_Item_Description') or '').strip().upper()
        if not erp:
            continue
        once[(get(r, 'Direct_JR_No'), erp)] = num(get(r, 'Produced_Quantity'))
    docs = {get(r, 'Direct_JR_No') for r in lines}
    return docs, len(lines), once


def check_day(d):
    iso = d.isoformat()
    t0 = time.time()
    ion = common.fetch(direct_jr, d, d)
    secs = time.time() - t0
    # iON records keep their own keys; keep only lines dated this day (as the sync does)
    ion = [r for r in ion if common.to_date(r.get('Direct_JR_Date')) == d]
    i_docs, i_lines, i_once = summarise(ion, lambda r, k: r.get(k))

    det = dash(f'/api/detail?from={iso}&to={iso}')
    h = det['headers']
    idx = {k: h.index(k) for k in h}
    rows = det['rows']
    d_docs, d_lines, d_once = summarise(rows, lambda r, k: r[idx[k]])

    pv = dash(f'/api/data?from={iso}&to={iso}')
    d_total = pv['daily_totals'].get(iso, 0.0)
    i_total = round(sum(i_once.values()), 3)

    problems = []
    if i_docs != d_docs:
        only_i, only_d = sorted(i_docs - d_docs), sorted(d_docs - i_docs)
        if only_i:
            problems.append(f'{len(only_i)} JR in iON missing on dashboard: {only_i[:8]}')
        if only_d:
            problems.append(f'{len(only_d)} JR on dashboard not in iON (deleted/changed?): {only_d[:8]}')
    if i_lines != d_lines:
        problems.append(f'line count iON={i_lines} dash={d_lines}')
    qty_diff = [(k, i_once[k], d_once.get(k)) for k in i_once
                if k in d_once and abs(i_once[k] - d_once[k]) > 0.001]
    if qty_diff:
        problems.append(f'{len(qty_diff)} JR/item qty differ, e.g. {qty_diff[:5]}')
    if abs(i_total - d_total) > 0.01:
        problems.append(f'total iON={i_total:,.3f} dash={d_total:,.3f}')

    status = 'MATCH' if not problems else 'DIFF'
    print(f'{iso}  iON: {len(i_docs):>4} JR {i_lines:>6} lines {i_total:>14,.2f} | '
          f'dashboard: {len(d_docs):>4} JR {d_lines:>6} lines {d_total:>14,.2f} | {status}'
          f'  ({secs:.0f}s)', flush=True)
    for p in problems:
        print('     -', p, flush=True)
    return not problems


def days(args):
    for a in args:
        if ':' in a:
            f, t = (date.fromisoformat(x) for x in a.split(':'))
            while f <= t:
                yield f
                f += timedelta(days=1)
        else:
            yield date.fromisoformat(a)


if len(sys.argv) < 2:
    sys.exit(__doc__)

results = [check_day(d) for d in days(sys.argv[1:])]
print(f'\n{sum(results)}/{len(results)} days match exactly', flush=True)
sys.exit(0 if all(results) else 1)
