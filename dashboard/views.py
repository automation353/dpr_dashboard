"""Views for the DPR dashboard.

- index     : render the single-page dashboard.
- api_data  : read the DPR sheet live and return the date-wise pivot (JSON).
- api_export: download the All Details rows for the dates as .xlsx.
- api_pull  : trigger the n8n DPR pull for a chosen date range, wait, return.
- healthz   : liveness probe.
"""

import io
import json
import logging
import re
from datetime import date

import requests
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font

from .sheet_reader import read_dpr_detail, read_dpr_rows

log = logging.getLogger(__name__)


def _iso(s):
    """Accept 'YYYY-MM-DD' or 'DD-MM-YYYY'/'DD/MM/YYYY'; return ISO or ''."""
    t = str(s or '').strip()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', t)
    if m:
        return t
    m = re.match(r'^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$', t)
    if m:
        return f'{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}'
    return ''


def _ddmmyyyy(iso):
    """ISO 'YYYY-MM-DD' -> 'DD-MM-YYYY' (the format iON / n8n expects)."""
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', str(iso or ''))
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}' if m else ''


def index(request):
    return render(request, 'dashboard/index.html', {
        'sheet_tab': settings.DPR_SHEET_TAB,
        'pull_enabled': bool(settings.DPR_PULL_WEBHOOK_URL),
    })


def api_data(request):
    from_iso = _iso(request.GET.get('from'))
    to_iso = _iso(request.GET.get('to'))
    data = read_dpr_rows(from_iso=from_iso or None, to_iso=to_iso or None)
    status = 200 if data.get('ok') else 502
    return JsonResponse(data, status=status)


def api_detail(request):
    """All columns, raw rows, for the 'all the data visible' detail table."""
    from_iso = _iso(request.GET.get('from'))
    to_iso = _iso(request.GET.get('to'))
    data = read_dpr_detail(from_iso=from_iso or None, to_iso=to_iso or None)
    status = 200 if data.get('ok') else 502
    return JsonResponse(data, status=status)


# Excel's sheet limit is 1,048,576 rows; a full month of DPR lines is ~100k.
_EXPORT_MAX_ROWS = 500000
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


def api_export(request):
    """Download the All Details table for the chosen dates as an .xlsx file."""
    from_iso = _iso(request.GET.get('from'))
    to_iso = _iso(request.GET.get('to'))
    data = read_dpr_detail(from_iso=from_iso or None, to_iso=to_iso or None,
                           max_rows=_EXPORT_MAX_ROWS)
    if not data.get('ok'):
        return JsonResponse(data, status=502)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet('All details')
    bold = Font(bold=True)
    header = []
    for h in data['headers']:
        c = WriteOnlyCell(ws, value=h)
        c.font = bold
        header.append(c)
    ws.freeze_panes = 'A2'
    ws.append(header)
    for row in data['rows']:
        out = []
        for i, v in enumerate(row):
            val = _xl_value(v)
            if isinstance(val, date):
                c = WriteOnlyCell(ws, value=val)
                c.number_format = 'DD-MM-YYYY'
                out.append(c)
            else:
                out.append(val)
        ws.append(out)

    buf = io.BytesIO()
    wb.save(buf)
    name = f'DPR_all_details_{from_iso or "start"}_to_{to_iso or "end"}.xlsx'
    resp = HttpResponse(
        buf.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="{name}"'
    if data.get('truncated'):
        resp['X-DPR-Truncated'] = f'first {_EXPORT_MAX_ROWS} rows'
    return resp


@require_http_methods(['POST'])
def api_pull(request):
    """Trigger the n8n DPR pull for a date range, waiting for it to finish.

    Body JSON: {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"} (also accepts
    DD-MM-YYYY). The chosen range is sent to n8n, which fetches exactly that
    window from iON and writes the sheet.
    """
    url = settings.DPR_PULL_WEBHOOK_URL
    if not url:
        return JsonResponse(
            {'ok': False, 'error': 'JCLP_DPR_PULL_WEBHOOK_URL not configured'},
            status=503,
        )

    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    from_iso = _iso(body.get('from'))
    to_iso = _iso(body.get('to'))
    if not from_iso or not to_iso:
        return JsonResponse(
            {'ok': False, 'error': 'both from and to dates are required'},
            status=400,
        )
    if from_iso > to_iso:
        from_iso, to_iso = to_iso, from_iso

    payload = {
        'source': 'dpr-dashboard',
        'fromDate': _ddmmyyyy(from_iso),
        'toDate': _ddmmyyyy(to_iso),
    }
    # The n8n flow responds immediately (ack) and then pulls iON + writes the
    # sheet in the background, so this call returns fast. The dashboard polls
    # the sheet afterwards to show rows as they land. A short timeout is enough
    # for the ack; it does NOT wait for the write to finish.
    ack_timeout = min(30, settings.DPR_PULL_TIMEOUT)
    try:
        resp = requests.post(url, json=payload, timeout=ack_timeout,
                             allow_redirects=True)
        ok = 200 <= resp.status_code < 300
        try:
            n8n = resp.json()
        except ValueError:
            n8n = {'body_preview': (resp.text or '')[:300]}
        return JsonResponse(
            {'ok': ok, 'started': ok, 'http_status': resp.status_code,
             'sent': payload, 'n8n': n8n},
            status=200 if ok else 502,
        )
    except requests.Timeout:
        # Ack didn't come back in time, but the n8n run was very likely started.
        return JsonResponse(
            {'ok': True, 'started': True,
             'note': 'ack timed out, but the pull was triggered; polling the sheet',
             'sent': payload},
            status=200,
        )
    except Exception as exc:                        # noqa: BLE001
        log.exception('DPR pull webhook failed')
        return JsonResponse({'ok': False, 'error': str(exc), 'sent': payload},
                            status=502)


def healthz(request):
    return JsonResponse({'ok': True, 'service': 'dpr-dashboard'})
