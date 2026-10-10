"""Bearer sessions for field users and admins."""

from __future__ import annotations

import time
from typing import Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import TOKEN_TTL_SECONDS
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
    if session is None or session.get("role") != role or session.get("exp", 0) < time.time():
        SESSIONS.pop(token, None)
        raise APIError(401, "Session expired. Please login again", "UNAUTHORIZED")
    return session


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
