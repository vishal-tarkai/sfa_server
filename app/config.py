"""Load env from the first existing file (no secret logging).

Search order (SFA_v2 monorepo or sfa_server clone):
  1) CMS/.env
  2) repo-root .env
  3) sfa_api/.env  (this folder when run locally or on Render)
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONO = ROOT.parent
PUBLIC_SUPABASE_ENV = ROOT / "supabase.public.env"
ENV_CANDIDATES = (
    MONO / "CMS" / ".env",
    MONO / ".env",
    ROOT / ".env",
)
ENV_PATH = next((path for path in ENV_CANDIDATES if path.exists()), ENV_CANDIDATES[0])


def load_env(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def reload_env_files() -> None:
    # Process env (Render) wins; files fill gaps via setdefault in load_env.
    for path in (
        PUBLIC_SUPABASE_ENV,
        *ENV_CANDIDATES,
        Path("/etc/secrets/.env"),
        Path("/etc/secrets/env"),
    ):
        if path.exists():
            load_env(path)


def supabase_credentials() -> tuple[str, str]:
    """Read Supabase settings from process env (Render injects before startup)."""
    url = _first_env("SUPABASE_URL").rstrip("/")
    key = _first_env("SUPABASE_ANON_KEY", "SUPABASE_KEY", "SUPABASE_ANON")
    return url, key


reload_env_files()
SUPABASE_URL, SUPABASE_ANON_KEY = supabase_credentials()
# Optional: FCM via Supabase Edge `send-fcm-notification` (see CMS/.env.example).
FCM_WEBHOOK_SECRET = _first_env("FCM_WEBHOOK_SECRET")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))

OFFICE_LAT = float(os.environ.get("ATTENDANCE_OFFICE_LAT", "30.512355"))
OFFICE_LNG = float(os.environ.get("ATTENDANCE_OFFICE_LNG", "76.807612"))
OFFICE_RADIUS_M = float(os.environ.get("ATTENDANCE_OFFICE_RADIUS_METERS", "500"))
# Wall-clock for attendance labels and attendance_date (Render defaults to UTC without this).
FIELD_TIMEZONE = os.environ.get("FIELD_TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# Temporary: skip geofence + serve env Head Office coords to the app (see supabase.public.env).
ATTENDANCE_SKIP_GEOFENCE = _env_bool("ATTENDANCE_SKIP_GEOFENCE", True)

FIELD_DEFAULT_PASSWORD = "1234"
ADMIN_EMAIL = "vishal@tarkai.com"
ADMIN_PASSWORD = "1234"
TOKEN_TTL_SECONDS = 86400
MIN_CHECKOUT_MINUTES = 180
PRESENT_MINUTES = 9 * 60
EXTENDED_MINUTES = 15 * 60
