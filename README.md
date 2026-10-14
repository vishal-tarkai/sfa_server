# SPINE SFA Field API (`sfa_api`)

**Canonical backend for the CMS mobile app.** All FastAPI / Render changes belong in this folder only.

- **Contract:** [`../CMS/API.md`](../CMS/API.md) (Part A field routes)
- **Deploy:** Render service `sfa-server-118k` tracks this repo (`main`)
- **Database:** Supabase (same project as `admin_flutter`)

## Run locally

```bash
cd sfa_api
cp .env.example .env   # SUPABASE_URL, SUPABASE_ANON_KEY
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run_local.sh
# or: uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

CMS `.env`:

```env
API_BASE_URL=http://127.0.0.1:8080/v1
```

Physical device on same Wi‑Fi: set `API_LAN_HOST` to your machine’s LAN IP (see `CMS/lib/core/api/api_config.dart`).

## Where to change code

| Area | Path |
|------|------|
| Field app (login, DPR, tour plan, attendance, …) | `app/routers/field.py` |
| Admin console | `app/routers/admin.py` |
| Shared helpers | `app/util.py`, `app/db.py` |

Do **not** duplicate API logic under `server/` — use **`sfa_api/`** only.

## Recent tour-plan approval behaviour

- `POST /tour-plans/submit-month` — body includes `reporting_manager_id` / `reporting_manager_name` → `approval_requests.forwarded_to`
- `GET /tour-approvals` — queue for logged-in senior (`forwarded_to` or legacy `users.manager` name match)
- `GET /tour-approvals/{approval_id}` — detail for senior review screen
- `GET /dpr/seniors` — reporting managers only (not all MRs)

Supabase backfill (legacy rows): `admin_flutter/supabase/migrations/034_*.sql`, `035_*.sql`
