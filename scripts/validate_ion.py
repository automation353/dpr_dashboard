"""Validate the DPR dashboard against TCS iON, day by day.

Two steps, both read-only:
  1. TCS iON = database = data API: runs the data project's own check,
     `/root/database/ion validate dpr` (it calls iON; nothing is stored).
  2. data API = this dashboard: for each day compares the dashboard's
     /api/data (produced qty, JR count) and /api/detail (line count) with the
     API's /agg/production and /count.

Usage (from the repo root, any python3):
  python3 scripts/validate_ion.py 2026-11-16                 # one day
  python3 scripts/validate_ion.py 2026-11-16 2026-11-22      # a range (from to)

Environment overrides: ION_CMD (default /root/database/ion), DPR_DASHBOARD_URL
(default http://127.0.0.1:8020), ENV_FILE (default /root/.env, for
JCLP_DPR_API_URL / JCLP_DPR_API_KEY). Exit code is 0 only when both steps match.

Note: today's data is stored only after the 20:15 sync, so check up to yesterday.
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta

ION = os.environ.get('ION_CMD', '/root/database/ion')
DASH = os.environ.get('DPR_DASHBOARD_URL', 'http://127.0.0.1:8020')
ENV_FILE = os.environ.get('ENV_FILE', '/root/.env')


def env(name):
    for line in open(ENV_FILE):
        if line.startswith(name + '='):
            return line.split('=', 1)[1].strip().strip('"\'')
    raise SystemExit(f'{name} not found in {ENV_FILE}')


def get(url):
    with urllib.request.urlopen(url, timeout=300) as r:
        return json.load(r)


def ddmmyyyy(d):
    return d.strftime('%d-%m-%Y')


def step1_ion(d_from, d_to):
    print(f'== Step 1: TCS iON = database = data API ({ION} validate dpr)\n', flush=True)
    proc = subprocess.run([ION, 'validate', 'dpr', '--from', ddmmyyyy(d_from),
                           '--to', ddmmyyyy(d_to)], capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    print(out, flush=True)
    return proc.returncode == 0 and 'ALL DAYS MATCH' in out


def step2_dashboard(d_from, d_to):
    print('== Step 2: data API = DPR dashboard\n', flush=True)
    api, key = env('JCLP_DPR_API_URL'), env('JCLP_DPR_API_KEY')
    dash = get(f'{DASH}/api/data?from={d_from}&to={d_to}')
    if not dash.get('ok'):
        print('dashboard /api/data failed:', dash.get('error'))
        return False
    print(f'{"date":<11}{"API lines":>10}{"dash lines":>11}{"API JR":>8}{"dash JR":>8}'
          f'{"API produced":>15}{"dash produced":>15}  result')
    ok_all, d = True, d_from
    while d <= d_to:
        iso = d.isoformat()
        q = urllib.parse.urlencode({'api_key': key, 'fromDate': ddmmyyyy(d), 'toDate': ddmmyyyy(d)})
        agg = get(f'{api}/agg/production?{q}')['data']
        a_lines = get(f'{api}/count?{q}')['count']
        a_qty = round(sum(r['produced_qty'] or 0 for r in agg), 3)
        a_jr = sum(r['jr_count'] or 0 for r in agg)
        d_lines = get(f'{DASH}/api/detail?from={iso}&to={iso}&page_size=1')['total']
        d_qty = dash['daily_totals'].get(iso, 0.0)
        d_jr = get(f'{DASH}/api/data?from={iso}&to={iso}').get('row_count') or 0
        ok = a_lines == d_lines and a_jr == d_jr and abs(a_qty - d_qty) < 0.01
        ok_all &= ok
        print(f'{iso:<11}{a_lines:>10,}{d_lines:>11,}{a_jr:>8,}{d_jr:>8,}'
              f'{a_qty:>15,.2f}{d_qty:>15,.2f}  {"MATCH" if ok else "DIFF"}', flush=True)
        d += timedelta(days=1)
    print(f'\nRESULT: {"ALL DAYS MATCH (data API = dashboard)" if ok_all else "SOME DAYS DIFFER"}')
    return ok_all


def main(argv):
    if not argv or len(argv) > 2:
        raise SystemExit(__doc__)
    d_from = date.fromisoformat(argv[0])
    d_to = date.fromisoformat(argv[-1])
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    ok1 = step1_ion(d_from, d_to)
    ok2 = step2_dashboard(d_from, d_to)
    print(f'\nOVERALL: {"iON = database = API = dashboard" if ok1 and ok2 else "MISMATCH - see above"}')
    return 0 if ok1 and ok2 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
