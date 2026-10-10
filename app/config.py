"""Load env from the first existing file (no secret logging).

Search order (SFA_v2 monorepo or sfa_server clone):
  1) CMS/.env
  2) repo-root .env
  3) server/.env  (this repo root when deployed standalone)
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONO = ROOT.parent
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


for _path in (
    *ENV_CANDIDATES,
    Path("/etc/secrets/.env"),
    Path("/etc/secrets/env"),
):
    if _path.exists():
        load_env(_path)

SUPABASE_URL = _first_env("SUPABASE_URL").rstrip("/")
SUPABASE_ANON_KEY = _first_env("SUPABASE_ANON_KEY", "SUPABASE_KEY", "SUPABASE_ANON")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))

OFFICE_LAT = float(os.environ.get("ATTENDANCE_OFFICE_LAT", "30.512355"))
OFFICE_LNG = float(os.environ.get("ATTENDANCE_OFFICE_LNG", "76.807612"))
OFFICE_RADIUS_M = float(os.environ.get("ATTENDANCE_OFFICE_RADIUS_METERS", "500"))

FIELD_DEFAULT_PASSWORD = "1234"
ADMIN_EMAIL = "vishal@tarkai.com"
ADMIN_PASSWORD = "1234"
TOKEN_TTL_SECONDS = 86400
MIN_CHECKOUT_MINUTES = 180
PRESENT_MINUTES = 9 * 60
EXTENDED_MINUTES = 15 * 60
