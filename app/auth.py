"""Bearer sessions for field users and admins."""

from __future__ import annotations

import time
from typing import Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import ADMIN_EMAIL, TOKEN_TTL_SECONDS
from app.errors import APIError

bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Paste the access_token from login. Swagger adds the Bearer prefix.",
)

SESSIONS: dict[str, dict[str, Any]] = {}


def issue_token(role: str, **data: Any) -> tuple[str, int]:
    now_ms = int(time.time() * 1000)
    if role == "field":
        token = f"cms_{data['employee_id']}_{now_ms}"
    else:
        token = f"admin_{now_ms}"
    SESSIONS[token] = {
        "role": role,
        "exp": time.time() + TOKEN_TTL_SECONDS,
        "token": token,
        **data,
    }
    return token, TOKEN_TTL_SECONDS


def drop_token(token: str | None) -> None:
    if token:
        SESSIONS.pop(token, None)


def _parse_field_token(token: str) -> tuple[str, float] | None:
    """`cms_{employee_id}_{issued_ms}` — survives server restarts (e.g. Render)."""
    if not token.startswith("cms_"):
        return None
    parts = token.split("_")
    if len(parts) < 3:
        return None
    try:
        issued_ms = int(parts[-1])
    except ValueError:
        return None
    employee_id = "_".join(parts[1:-1]).strip()
    if not employee_id:
        return None
    return employee_id, issued_ms / 1000.0


def _parse_admin_token(token: str) -> float | None:
    if not token.startswith("admin_"):
        return None
    suffix = token.removeprefix("admin_")
    try:
        issued_ms = int(suffix)
    except ValueError:
        return None
    return issued_ms / 1000.0


def _restore_session_from_token(token: str, role: str) -> dict[str, Any] | None:
    now = time.time()
    if role == "field":
        parsed = _parse_field_token(token)
        if not parsed:
            return None
        employee_id, issued_at = parsed
        exp = issued_at + TOKEN_TTL_SECONDS
        if exp < now:
            return None
        return {
            "role": "field",
            "exp": exp,
            "token": token,
            "employee_id": employee_id,
        }
    if role == "admin":
        issued_at = _parse_admin_token(token)
        if issued_at is None:
            return None
        exp = issued_at + TOKEN_TTL_SECONDS
        if exp < now:
            return None
        return {
            "role": "admin",
            "exp": exp,
            "token": token,
            "email": ADMIN_EMAIL,
        }
    return None


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise APIError(401, "Session expired. Please login again", "UNAUTHORIZED")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise APIError(401, "Session expired. Please login again", "UNAUTHORIZED")
    return token


def _session(authorization: str | None, role: str) -> dict[str, Any]:
    token = _bearer(authorization)
    session = SESSIONS.get(token)
    if (
        session is not None
        and session.get("role") == role
        and session.get("exp", 0) >= time.time()
    ):
        return session

    restored = _restore_session_from_token(token, role)
    if restored is not None:
        SESSIONS[token] = restored
        return restored

    SESSIONS.pop(token, None)
    raise APIError(401, "Session expired. Please login again", "UNAUTHORIZED")


def _authorization_header(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str | None:
    if credentials is None or not credentials.credentials:
        return None
    return f"Bearer {credentials.credentials}"


def field_user(
    authorization: str | None = Depends(_authorization_header),
) -> dict[str, Any]:
    return _session(authorization, "field")


def admin_user(
    authorization: str | None = Depends(_authorization_header),
) -> dict[str, Any]:
    return _session(authorization, "admin")
