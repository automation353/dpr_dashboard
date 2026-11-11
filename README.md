# DPR Dashboard

A **standalone** Django app that shows **Direct Job Receipt (DPR) daily production**
date-wise, and pulls fresh DPR data from TCS iON into the Google Sheet on demand
via n8n. Fully separate from the jcpl portal — its own project, settings and port.

## What it does
- **Reads** the DPR Google Sheet tab live (read-only CSV export — nothing stored
  on the server) and pivots it into an **item × date** grid of produced quantity.
  Produced qty is counted **once per Direct JR** (it repeats on every RM line).
- **Pulls** on demand: pick a **From/To** date range and click *Pull from iON →
  Sheet*. The dashboard POSTs the range to the n8n DPR webhook, which fetches
  exactly that window from iON and writes the sheet; the view then refreshes.

## Configuration (all in the shared `/root/.env`, namespaced `JCLP_DPR_*`)
| Key | Meaning |
|---|---|
| `JCLP_DPR_SHEET_ID` | ID of the DPR Google Sheet (the dharmik fetch-and-dump flow's sheet) |
| `JCLP_DPR_SHEET_TAB` | tab name holding the per-row DPR detail (default `DPR all Plant`) |
| `JCLP_DPR_PULL_WEBHOOK_URL` | n8n webhook (production URL) that pulls iON for a date range |
| `JCLP_DPR_PULL_TIMEOUT` | seconds to wait for the pull (default `300`) |
| `JCLP_DPR_DJANGO_SECRET_KEY` | Django secret (a dev fallback exists) |
| `JCLP_DPR_DJANGO_DEBUG` | `True`/`False` |
| `JCLP_DPR_DJANGO_ALLOWED_HOSTS` | comma-separated hosts (default `*`) |

The iON `servicekey`/`u` secrets live **only inside n8n** — this app never sees them.

## Run (own, self-contained virtualenv — no link to the jcpl portal)
```bash
cd /root/jclp_automation_portal/dpr
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
.venv/bin/python manage.py migrate
.venv/bin/gunicorn dpr_portal.wsgi:application --bind 0.0.0.0:8020 --workers 2
```
This project shares nothing with the jcpl portal — its own venv, settings, DB,
port (8020) and process. The only server-wide file both use is `/root/.env`
(the box's single shared env by convention); DPR reads only its `JCLP_DPR_*` keys.
Then open http://<host>:8020/ . Health probe: `/healthz`.

## Endpoints
- `GET /` — the dashboard.
- `GET /api/data?from=YYYY-MM-DD&to=YYYY-MM-DD` — date-wise pivot JSON.
- `POST /api/pull` — body `{"from":"YYYY-MM-DD","to":"YYYY-MM-DD"}`; triggers the n8n pull.

## n8n side (dharmik flow `PPC R3SS + DPR (GET)`, id `puJp2VDGiIru4hjW`)
A **Webhook** trigger feeds the **DPR Date Window** node, which uses the posted
`fromDate`/`toDate` when present and otherwise falls back to month-to-date (so the
21:00 schedule is unchanged). A **Respond to Webhook** node returns `{ok, rows}`.
