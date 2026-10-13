# Rent a Biker — full app (backend scaffold, milestone 1)

Internal build workspace. This is NOT the live static app (artifact slug
`rent-a-biker`) — do not point customers here.

## Stack
- Python 3.12 + FastAPI (API + serves the web frontend)
- SQLAlchemy 2.0 + SQLite (local dev; Postgres later at launch)
- JWT bearer-token auth, bcrypt password hashing
- Frontend: plain mobile-first HTML/CSS/JS in `app/static/` (no build step)

## Run locally
```bash
cd hidden_files/src
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8471
```
- Health: http://127.0.0.1:8471/health
- API docs: http://127.0.0.1:8471/docs
- Placeholder frontend: http://127.0.0.1:8471/

## Env vars (all optional in dev)
- `RAB_SECRET_KEY` — JWT signing key. Dev default is insecure; set a real one before any launch.
- `RAB_DATABASE_URL` — defaults to `sqlite:///./rentabiker.db` in this dir. On Render this is wired to Postgres automatically (`postgres://` → `postgresql+psycopg://`); psycopg is in requirements.txt.
- `RAB_UPLOAD_DIR` / `RAB_MAX_PHOTO_BYTES` — bike photo storage dir and size cap (5 MB). On Render this points at the persistent disk (`/opt/render/project/src/uploads`).
- `RAB_GEOCODER_*` — free OpenStreetMap Nominatim geocoder (base URL, User-Agent,
  timeout, min seconds between requests; defaults honor its usage policy).

## Deploying (Kenneth's approval only — never automated)
- `../render.yaml` (repo root at push time) — Render Blueprint: web service + Postgres + uploads disk. Deploy path and the ordered Kenneth-only steps live in `../PRODUCTION_CHECKLIST.md`.

## Business rules baked in
- Rides: $2.50/mile (`app/fares.py`)
- Events: $75/hour, 2-hour minimum
- Commission: biker keeps 75%, Kenneth/platform 25% (record-keeping only for now;
  payouts stay manual via PayPal until Stripe Connect is approved)
