# DPR Dashboard

A **standalone** Django app that shows **Direct Job Receipt (DPR) daily production**
date-wise. It reads live from the company **data API** (`database-api`, the
`/root/database` project over PostgreSQL), with no Google Sheet and no n8n. It is fully
separate from the jcpl portal: its own repo, venv, settings, port and process.

## Where the data comes from
1. **Nightly sync** (`/root/database`, `ion sync all`, cron **20:15** from
   `/etc/cron.d/jcpl-database`) pulls Direct JR lines from TCS iON into PostgreSQL
   (database `dpr`, table `direct_jr_lines`). Each night re-reads the **last 7 days**,
   so edits made in iON within a week are picked up.
2. **database-api** (gunicorn, port 8030; also answers on the old port 8010 and the old
   paths) serves that table: `/api/direct_jr_lines` (paged rows), `/count`, and
   `/agg/production` (date x item totals computed in PostgreSQL).
3. **This dashboard** (port 8020) calls the data API only. Produced qty is counted
   **once per Direct JR + item + date** (it repeats on every raw-material line).

All six JCPL sites are fetched (JCPL01, 02, 03, 05, 06, 07) from 1 April 2026. Only
JCPL01 and JCPL02 have Direct JR (production) entries in iON; the others return none.
The **Site** filter on the page (All sites / one site) applies to the day-wise view,
All Details and the Excel export (`site=` on every endpoint; list in `JCLP_DPR_SITES`).
The **Item type** filter (Finished Goods, Semi-finished Goods, ...; `type=`) and the
**Item group** filter (groups produced in the chosen range/site/type, from the stock
statement; `group=`) apply to the day-wise view (`group=` on `/api/data`, which also returns the
`item_types` and `item_groups` lists) and the Excel export (`"type"`, `"group"` in the start
body; other rows are skipped while streaming). All Details is not filtered by type/group yet: the data API can
only filter raw rows on stored columns, and the item group is not stored in `direct_jr_lines`. Today's data shows
up after that evening's sync. The data project is documented in `/root/database/README.md`.

## What it shows
- **Day-wise**: an item x date grid of produced quantity with daily and grand totals.
- **Month-wise by group**: produced quantity per item group per month, with item count,
  monthly totals and grand total (same Site / Item type / Item group filters;
  `months`, `group_months`, `month_totals` in `/api/data`).
- **All Details**: every raw line with all columns, 500 rows per page, plus four item
  columns after the ERP code: `Item_Type`, `Item_Category`, `Item_Group`,
  `Item_Group_Description`. iON's Direct JR report has no item group; the data API looks
  it up from the stock statement and returns it on `/agg/production`, which the dashboard
  reads for the same dates/site (the Excel export includes them too).
- **Export to Excel**: All Details for any chosen range as one `.xlsx` (more than 1,000,000 rows continue on extra sheets).

## Configuration (shared `/root/.env`, namespaced `JCLP_DPR_*`)
| Key | Meaning |
|---|---|
| `JCLP_DPR_API_URL` | data API table URL, `http://127.0.0.1:8010/api/direct_jr_lines` |
| `JCLP_DPR_API_KEY` | the data API's dpr key (sent as `api_key`) |
| `JCLP_DPR_API_PAGE_SIZE` | rows per API page when streaming (default `10000`) |
| `JCLP_DPR_SITES` | sites in the Site filter (default `JCPL01,JCPL02,JCPL03,JCPL05,JCPL06,JCPL07`) |
| `JCLP_DPR_DJANGO_SECRET_KEY` | Django secret (a dev fallback exists; set one in production) |
| `JCLP_DPR_DJANGO_DEBUG` | `True`/`False` |
| `JCLP_DPR_DJANGO_ALLOWED_HOSTS` | comma-separated hosts |

The iON `servicekey`/`u` secrets live only in `/root/database/.env`; this app never sees them.

## Run
```bash
cd /root/jclp_automation_portal/dpr
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
.venv/bin/python manage.py migrate
```
In production it runs as the systemd unit `dpr-gunicorn` (`systemd/dpr-gunicorn.service`,
2 workers on `0.0.0.0:8020`), with resource caps from `systemd/dpr-gunicorn-limits.conf`
(install as `/etc/systemd/system/dpr-gunicorn.service.d/limits.conf`). Health: `/healthz`.
The data API (`database-api`) belongs to the `/root/database` project and is managed there.

**Public URL: https://dpr.jcplportal.tech** (nginx `nginx/dpr.conf`, installed as
`/etc/nginx/sites-enabled/zz-dpr` so jcpl stays the default HTTPS site; certificate via
certbot webroot `/var/www/dpr-acme`, auto-renewed). Use HTTPS: Chrome holds downloads
from plain-http pages as "Unconfirmed". `http://<host>:8020/` still works directly.

## Endpoints
- `GET /`: the dashboard.
- `GET /api/data?from=YYYY-MM-DD&to=YYYY-MM-DD&site=JCPL02`: date-wise pivot JSON (`site` optional = all).
- `GET /api/detail?from&to&site&page=1&page_size=500`: one page of raw lines (max 2000 per page).
- `POST /api/export/start` with body `{"from","to","site"}`: starts an Excel export and returns `{id,total}`.
  It returns 409 if another export is running, or 400 for a bad or too-long range.
- `GET /api/export/status?id=...`: progress (`queued|running|saving|done|error|cancelled`).
- `POST /api/export/cancel` with body `{"id"}`: stops a queued or running export (the page's
  *Cancel export* button); the job stops within ~1 s and its partial file is deleted.
- `GET /api/export/download?id=...`: the finished `.xlsx`.
- `POST /api/pull`: disabled (the old n8n pull); returns 503.

## Validating against TCS iON
`scripts/validate_ion.py` checks the whole chain day by day, read-only:
1. **TCS iON = database = data API**, by running the data project's own check
   (`/root/database/ion validate dpr`), which compares lines, documents, produced and
   consumed qty per day;
2. **data API = dashboard**: per day, lines (`/count` vs `/api/detail`), Direct JR count
   and produced qty (`/agg/production` vs `/api/data`).
```bash
python3 scripts/validate_ion.py 2026-11-16 2026-11-22   # from to (or a single date)
```
Exit code is 0 only if both steps match. Check up to yesterday: today's rows are stored
after the 20:15 sync. Last checked 23 Nov 2026: Nov 16-22 all match.

## Server-safety design (any date range)
- **Day-wise view** uses the data API `/agg/production` totals (computed in PostgreSQL):
  any range, even years, is one ~1 s request.
- **All Details** is paged in the database: 500 rows per page (`/api/detail?page=N`).
- **Excel export** is a background job (`/api/export/start|status|download`): any range,
  one export at a time, run as a systemd unit capped at `CPUQuota=50%`,
  `MemoryMax=1G`, `Nice=10`, with a short pause between API pages. The file is written by
  a small streaming XLSX writer (`dashboard/xlsx_stream.py`, ~10x faster than openpyxl) while
  the next API page is fetched in parallel. April -> November (~590k rows, 75 MB, JCPL01 only at the time) built in
  ~2 min with jcpl response times unchanged. Files live in `exports/`, deleted after a day.
