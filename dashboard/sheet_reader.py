"""Read DPR lines from the database API and pivot them date-wise.

Data source: the dpr-api `direct_jr_lines` endpoint (JCLP_DPR_API_URL), paged.
The historical notes below describe the original Google Sheet source; the
rows have the same Direct JR x Raw Material shape, so the logic is unchanged.


The dharmik fetch-and-dump n8n flow writes one row per DPR detail line
(Direct JR x Raw Material) into a Google Sheet tab. Each row carries the
Direct JR date and the produced quantity. This module reads that tab straight
from the sheet (read-only gviz/export CSV — never a write, no service account)
and reshapes it into an item x date grid for the dashboard.

Key correctness rule (from the DPR pipeline docs): Produced_Quantity REPEATS on
every raw-material line of the same Direct JR. So we count produced qty ONCE per
(Direct_JR_No, item, date) before summing — summing raw rows would multi-count
production by the number of raw materials each JR consumes.
"""

import logging
import re
from datetime import date

import requests
from django.conf import settings

log = logging.getLogger(__name__)


def _norm(s):
    """Normalise a header: lowercase, strip, collapse spaces/underscores."""
    return re.sub(r'[\s_]+', ' ', str(s or '').strip().lower())


# Candidate header labels (normalised) for each logical column. The sheet is a
# raw dump, so we match tolerantly rather than assume exact names.
_DATE_KEYS = ('direct jr date', 'jr date', 'date', 'production date', 'dir jr date')
_ERP_KEYS = ('erp code', 'erp', 'finished item description', 'item code', 'erp item code')
_FG_KEYS = ('fg item code', 'fg code', 'finished item code', 'fg', 'jolly code')
_QTY_KEYS = ('produced qty', 'produced quantity', 'production qty', 'qty', 'quantity',
             'produced')
_JR_KEYS = ('direct jr no', 'jr no', 'direct jr number', 'jr', 'direct jr')


def _pick(headers_norm, candidates):
    """Return the index of the first header that matches a candidate.

    Prefers an exact match, then a header that starts with / contains a
    candidate (so "Produced Qty (Nos)" still maps to produced qty).
    """
    for want in candidates:                       # exact first
        for i, h in enumerate(headers_norm):
            if h == want:
                return i
    for want in candidates:                       # then contains
        for i, h in enumerate(headers_norm):
            if want in h:
                return i
    return None


def _to_iso(s):
    """Parse a date in DD-MM-YYYY / DD/MM/YYYY / YYYY-MM-DD to ISO YYYY-MM-DD.

    Returns '' when the value isn't a recognisable date.
    """
    t = str(s or '').strip()
    if not t:
        return ''
    # YYYY-MM-DD (already ISO) or YYYY/MM/DD
    m = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', t)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return f'{y}-{mo:02d}-{d:02d}'
    # DD-MM-YYYY or DD/MM/YYYY (iON format). Handles optional time suffix.
    m = re.match(r'^(\d{1,2})[-/](\d{1,2})[-/](\d{4})', t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        return f'{y}-{mo:02d}-{d:02d}'
    return ''


def _num(v):
    """Parse a possibly comma-grouped number; non-numbers become 0.0."""
    try:
        return float(str(v if v is not None else '').replace(',', '').strip() or 0)
    except ValueError:
        return 0.0


def _ddmmyyyy(iso):
    """ISO 'YYYY-MM-DD' -> 'DD-MM-YYYY' (the format the DPR API expects)."""
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', str(iso or ''))
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}' if m else ''


def _cellstr(v):
    """API JSON value -> the string a CSV cell would have held."""
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _fetch_api_rows(from_iso=None, to_iso=None, timeout=60, max_rows=None):
    """Fetch DPR lines from the database API as a table: [headers, row, row, ...].

    Returns the same shape csv.reader gave for the sheet, so everything
    downstream (header detection, dedupe, pivot) is unchanged. Pages through
    the API (limit/offset) until a page comes back short. With no date range,
    defaults to month-to-date so a blank filter can't pull the whole table.
    Raises on config/HTTP error.
    """
    url = settings.DPR_API_URL
    key = settings.DPR_API_KEY
    if not url or not key:
        raise RuntimeError('JCLP_DPR_API_URL / JCLP_DPR_API_KEY not configured in /root/.env')

    if not from_iso and not to_iso:
        today = date.today()
        from_iso, to_iso = today.replace(day=1).isoformat(), today.isoformat()

    page = settings.DPR_API_PAGE_SIZE
    params = {'api_key': key, 'limit': page}
    if from_iso:
        params['fromDate'] = _ddmmyyyy(from_iso)
    if to_iso:
        params['toDate'] = _ddmmyyyy(to_iso)

    headers, rows, seen_ids, offset = None, [], set(), 0
    while True:
        params['offset'] = offset
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
        data = body.get('data') or []
        for rec in data:
            rid = rec.get('id')
            if rid is not None:
                if rid in seen_ids:
                    continue          # guard against overlap between pages
                seen_ids.add(rid)
            if headers is None:
                headers = list(rec.keys())
            rows.append([_cellstr(rec.get(h)) for h in headers])
        if len(data) < page or (max_rows and len(rows) >= max_rows):
            break
        offset += page

    return [headers] + rows if headers else []


def read_dpr_rows(from_iso=None, to_iso=None, timeout=30):
    """Read the DPR tab and return an item x date pivot for [from_iso, to_iso].

    from_iso/to_iso are 'YYYY-MM-DD' or None (no bound on that side).

    Returns a dict:
      {
        "ok": bool,
        "dates": ["YYYY-MM-DD", ...],              # sorted, within range, that have data
        "items": [{"erp_code","fg_item_code","by_date":{iso:qty},"total"}],
        "daily_totals": {iso: qty},
        "grand_total": float,
        "row_count": int,                          # distinct (jr,item,date) counted
        "columns": {...},                          # which headers were detected (for debugging)
        "error": str|None,
      }
    """
    try:
        rows = _fetch_api_rows(from_iso, to_iso, timeout=max(timeout, 60))
    except Exception as exc:                        # noqa: BLE001 - surface to UI
        log.exception('DPR API fetch failed')
        return _empty(error=str(exc))

    if not rows:
        return _empty()   # no DPR lines in this date range

    # Find the header row: the row (scanning the top) that matches the most of
    # our known logical columns. A raw dump usually has the header on row 0, but
    # this tolerates a banner/blank row above it.
    hdr_idx, best = 0, -1
    for i, r in enumerate(rows[:10]):
        rn = [_norm(c) for c in r]
        score = sum(1 for keys in (_DATE_KEYS, _ERP_KEYS, _QTY_KEYS)
                    if _pick(rn, keys) is not None)
        if score > best:
            best, hdr_idx = score, i
    headers_norm = [_norm(c) for c in rows[hdr_idx]]

    i_date = _pick(headers_norm, _DATE_KEYS)
    i_erp = _pick(headers_norm, _ERP_KEYS)
    i_fg = _pick(headers_norm, _FG_KEYS)
    i_qty = _pick(headers_norm, _QTY_KEYS)
    i_jr = _pick(headers_norm, _JR_KEYS)

    cols = {
        'header_row': hdr_idx,
        'date': rows[hdr_idx][i_date] if i_date is not None else None,
        'erp_code': rows[hdr_idx][i_erp] if i_erp is not None else None,
        'fg_item_code': rows[hdr_idx][i_fg] if i_fg is not None else None,
        'produced_qty': rows[hdr_idx][i_qty] if i_qty is not None else None,
        'direct_jr_no': rows[hdr_idx][i_jr] if i_jr is not None else None,
    }

    if i_qty is None or i_erp is None:
        return _empty(
            error='could not locate the item (erp_code) / quantity columns in '
                  'the sheet header (detected: %r)' % cols,
            columns=cols,
        )

    # The date column is optional: the current flow writes a month total per
    # item (no date), so we degrade to totals-only and flag has_dates=False.
    # Once the flow emits a date, the same reader produces the day-by-day grid.
    has_dates = i_date is not None

    def cell(r, idx):
        return r[idx] if (idx is not None and idx < len(r)) else ''

    # Aggregate, deduping produced qty once per (jr, erp, date). seen_jr tracks
    # which (jr, erp, date) we've already counted; without a JR column we sum
    # every row (the flow already de-duplicated per JR before writing).
    agg = {}                 # erp_code -> {"fg": str, "by_date": {iso: qty}, "total": qty}
    seen_jr = set()
    counted = 0

    for r in rows[hdr_idx + 1:]:
        iso = _to_iso(cell(r, i_date)) if has_dates else None
        if has_dates:
            if not iso:
                continue
            if from_iso and iso < from_iso:
                continue
            if to_iso and iso > to_iso:
                continue
        erp = str(cell(r, i_erp)).strip().upper()
        if not erp:
            continue
        fg = str(cell(r, i_fg)).strip()
        qty = _num(cell(r, i_qty))
        jr = str(cell(r, i_jr)).strip() if i_jr is not None else ''

        if i_jr is not None:
            key = (jr, erp, iso)
            if key in seen_jr:
                continue          # same JR's other RM lines — produced already counted
            seen_jr.add(key)

        slot = agg.setdefault(erp, {'fg': fg, 'by_date': {}, 'total': 0.0})
        if fg and not slot['fg']:
            slot['fg'] = fg
        slot['total'] += qty
        if iso:
            slot['by_date'][iso] = slot['by_date'].get(iso, 0.0) + qty
        counted += 1

    # Shape the output.
    date_set = set()
    for v in agg.values():
        date_set.update(v['by_date'].keys())
    dates = sorted(date_set)

    daily_totals = {d: 0.0 for d in dates}
    items = []
    for erp in sorted(agg.keys()):
        by_date = agg[erp]['by_date']
        total = agg[erp]['total']
        for d, q in by_date.items():
            daily_totals[d] += q
        items.append({
            'erp_code': erp,
            'fg_item_code': agg[erp]['fg'],
            'by_date': {d: round(by_date.get(d, 0.0), 3) for d in dates},
            'total': round(total, 3),
        })

    # Heaviest producers first — more useful than alphabetical on a wide grid.
    items.sort(key=lambda it: it['total'], reverse=True)

    grand_total = round(sum(it['total'] for it in items), 3)
    return {
        'ok': True,
        'has_dates': has_dates,
        'dates': dates,
        'items': items,
        'daily_totals': {d: round(v, 3) for d, v in daily_totals.items()},
        'grand_total': grand_total,
        'row_count': counted,
        'columns': cols,
        'error': None,
    }


def _empty(error=None, columns=None):
    return {
        'ok': error is None,
        'has_dates': False,
        'dates': [],
        'items': [],
        'daily_totals': {},
        'grand_total': 0.0,
        'row_count': 0,
        'columns': columns or {},
        'error': error,
    }


_MAX_DETAIL_ROWS = 20000


def read_dpr_detail(from_iso=None, to_iso=None, timeout=30):
    """Return the raw DPR rows (ALL columns) for the date range — the
    "all the data visible" detail table.

    Column-agnostic: it passes through whatever headers the sheet has, and
    only filters by date when a date column is detectable. Returns:
      {"ok", "headers": [...], "rows": [[...], ...], "date_col": int|None,
       "row_count": int, "truncated": bool, "error": str|None}
    """
    try:
        all_rows = _fetch_api_rows(from_iso, to_iso, timeout=max(timeout, 60),
                                   max_rows=_MAX_DETAIL_ROWS + 1)
    except Exception as exc:                        # noqa: BLE001
        log.exception('DPR API fetch failed')
        return {'ok': False, 'headers': [], 'rows': [], 'date_col': None,
                'row_count': 0, 'truncated': False, 'error': str(exc)}

    if not all_rows:
        return {'ok': True, 'headers': [], 'rows': [], 'date_col': None,
                'row_count': 0, 'truncated': False, 'error': None}

    # Header = the row (scanning the top) with the most non-empty cells; a raw
    # dump has it on row 0. This keeps the detail view honest to the sheet.
    hdr_idx, best = 0, -1
    for i, r in enumerate(all_rows[:10]):
        n = sum(1 for c in r if str(c).strip())
        if n > best:
            best, hdr_idx = n, i
    headers = all_rows[hdr_idx]
    headers_norm = [_norm(c) for c in headers]
    i_date = _pick(headers_norm, _DATE_KEYS)

    out = []
    truncated = False
    for r in all_rows[hdr_idx + 1:]:
        if not any(str(c).strip() for c in r):
            continue
        if i_date is not None and (from_iso or to_iso):
            iso = _to_iso(r[i_date]) if i_date < len(r) else ''
            if iso:
                if from_iso and iso < from_iso:
                    continue
                if to_iso and iso > to_iso:
                    continue
        # Normalise width to the header so the table lines up.
        row = list(r) + [''] * (len(headers) - len(r))
        out.append(row[:len(headers)])
        if len(out) >= _MAX_DETAIL_ROWS:
            truncated = True
            break

    return {
        'ok': True,
        'headers': headers,
        'rows': out,
        'date_col': i_date,
        'row_count': len(out),
        'truncated': truncated,
        'error': None,
    }
