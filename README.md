# DPR Dashboard

A **standalone** Django app that shows **Direct Job Receipt (DPR) daily production**
date-wise. It reads live from the **DPR database API** (`dpr-api`, FastAPI over
PostgreSQL in `/opt/dpr_sync`), with no Google Sheet and no n8n. It is fully
separate from the jcpl portal: its own repo, venv, settings, port and process.

## Where the data comes from
1. **Nightly sync** (`/opt/dpr_sync/run.py --all`, cron 20:00) pulls the last
   3 days of Direct JR lines from TCS iON into PostgreSQL (`direct_jr_lines`).
2. **dpr-api** (port 8010) serves that table: `/api/direct_jr_lines` (paged rows),
   `/count`, and `/agg/production` (date x item totals computed in PostgreSQL).
3. **This dashboard** (port 8020) calls dpr-api only. Produced qty is counted
   **once per Direct JR + item + date** (it repeats on every raw-material line).

Today's data shows up after that evening's sync.

## What it shows
- **Day-wise**: an item x date grid of produced quantity with daily and grand totals.
- **All Details**: every raw line with all columns, 500 rows per page.
- **Export to Excel**: All Details for any chosen range as one `.xlsx` (more than 1,000,000 rows continue on extra sheets).

## Configuration (shared `/root/.env`, namespaced `JCLP_DPR_*`)
| Key | Meaning |
|---|---|
| `JCLP_DPR_API_URL` | dpr-api table URL, e.g. `http://127.0.0.1:8010/api/direct_jr_lines` |
| `JCLP_DPR_API_KEY` | dpr-api key (sent as `api_key`) |
| `JCLP_DPR_API_PAGE_SIZE` | rows per API page when streaming (default `10000`) |
| `JCLP_DPR_DJANGO_SECRET_KEY` | Django secret (a dev fallback exists; set one in production) |
| `JCLP_DPR_DJANGO_DEBUG` | `True`/`False` |
| `JCLP_DPR_DJANGO_ALLOWED_HOSTS` | comma-separated hosts |

The iON `servicekey`/`u` secrets live only in `/opt/dpr_sync/.env`; this app never sees them.

## Run
```bash
cd /root/jclp_automation_portal/dpr
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
.venv/bin/python manage.py migrate
```
In production it runs as the systemd unit `dpr-gunicorn` (`systemd/dpr-gunicorn.service`,
2 workers on `0.0.0.0:8020`), with resource caps from `systemd/dpr-gunicorn-limits.conf`
(install as `/etc/systemd/system/dpr-gunicorn.service.d/limits.conf`). dpr-api has its
own caps in `systemd/dpr-api-limits.conf`. Health: `/healthz`.

**Public URL: https://dpr.jcplportal.tech** (nginx `nginx/dpr.conf`, installed as
`/etc/nginx/sites-enabled/zz-dpr` so jcpl stays the default HTTPS site; certificate via
certbot webroot `/var/www/dpr-acme`, auto-renewed). Use HTTPS: Chrome holds downloads
from plain-http pages as "Unconfirmed". `http://<host>:8020/` still works directly.

## Endpoints
- `GET /`: the dashboard.
- `GET /api/data?from=YYYY-MM-DD&to=YYYY-MM-DD`: date-wise pivot JSON.
- `GET /api/detail?from&to&page=1&page_size=500`: one page of raw lines (max 2000 per page).
- `POST /api/export/start` with body `{"from","to"}`: starts an Excel export and returns `{id,total}`.
  It returns 409 if another export is running, or 400 for a bad or too-long range.
- `GET /api/export/status?id=...`: progress (`queued|running|saving|done|error`).
- `GET /api/export/download?id=...`: the finished `.xlsx`.
- `POST /api/pull`: disabled (the old n8n pull); returns 503.

## Validating against TCS iON
`scripts/validate_ion.py` checks the dashboard against TCS iON day by day. It
calls iON read-only through `/opt/dpr_sync` (no database writes) and compares,
per day: the Direct JR numbers, the line count, the produced qty per JR/item
(counted once per JR) and the daily total.
```bash
python3 scripts/validate_ion.py 2026-10-01:2026-11-11   # system python3 (needs psycopg2)
```
Each day prints `MATCH` or `DIFF` with the reason; exit code is 0 only if all
days match. The nightly sync re-reads just the last 3 days, so run this over
older ranges to catch JRs edited or cancelled in iON after they were synced.

## Server-safety design (any date range)
- **Day-wise view** uses the dpr-api `/agg/production` totals (computed in PostgreSQL):
  any range, even years, is one ~1 s request.
- **All Details** is paged in the database: 500 rows per page (`/api/detail?page=N`).
- **Excel export** is a background job (`/api/export/start|status|download`): any range,
  one export at a time, run as a systemd unit capped at `CPUQuota=50%`,
  `MemoryMax=1G`, `Nice=10`, with pauses between API pages. Files live in `exports/`
  and are deleted after a day. A month (~94k rows) takes ~3 min at ~20% server CPU.
