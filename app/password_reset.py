"""Forgot / reset password for field users (email OTP)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from app.config import PASSWORD_RESET_DEV_LOG, PASSWORD_RESET_PEPPER, PASSWORD_RESET_TTL_MINUTES
from app.db import DB
from app.emailer import send_password_reset_email, smtp_configured
from app.errors import APIError

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CODE_RE = re.compile(r"^\d{6}$")
_MIN_PASSWORD_LEN = 4
_MAX_PASSWORD_LEN = 128


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _hash_code(email: str, code: str) -> str:
    payload = f"{_normalize_email(email)}:{code}:{PASSWORD_RESET_PEPPER}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _find_active_user_by_email(db: DB, email: str) -> dict | None:
    wanted = _normalize_email(email)
    rows = await db.select("users", filters={"status": "active"})
    for row in rows:
        row_email = _normalize_email(str(row.get("email") or ""))
        if row_email and row_email == wanted:
            return row
    return None


async def _invalidate_pending(db: DB, employee_id: str) -> None:
    try:
        await db.delete("password_reset_requests", {"employee_id": employee_id})
    except APIError:
        pass


async def request_password_reset(db: DB, email: str) -> None:
    raw = (email or "").strip()
    if not raw or not _EMAIL_RE.match(raw):
        raise APIError(
            422,
            "Please enter a valid email address",
            "VALIDATION_ERROR",
            {"email": ["Please enter a valid email address"]},
        )

    user = await _find_active_user_by_email(db, raw)
    if user is None:
        raise APIError(
            404,
            "No account found with this email address.",
            "NOT_FOUND",
            {"email": ["No account found with this email address."]},
        )

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES)
    employee_id = str(user.get("employee_id") or "")
    normalized = _normalize_email(raw)

    await _invalidate_pending(db, employee_id)
    await db.insert(
        "password_reset_requests",
        {
            "employee_id": employee_id,
            "email": normalized,
            "code_hash": _hash_code(normalized, code),
            "expires_at": expires.isoformat(),
        },
    )

    if smtp_configured():
        name = str(user.get("name") or "")
        await asyncio.to_thread(
            send_password_reset_email,
            normalized,
            name,
            code,
            PASSWORD_RESET_TTL_MINUTES,
        )
    elif PASSWORD_RESET_DEV_LOG:
        logger.warning(
            "PASSWORD_RESET_DEV_LOG: reset code for %s (%s) is %s",
            normalized,
            employee_id,
            code,
        )
    else:
        raise APIError(
            503,
            "Password reset email is not configured. Contact your administrator.",
            "SERVICE_UNAVAILABLE",
        )


async def complete_password_reset(db: DB, email: str, code: str, password: str) -> None:
    raw_email = (email or "").strip()
    raw_code = (code or "").strip()
    if not raw_email or not _EMAIL_RE.match(raw_email):
        raise APIError(
            422,
            "Please enter a valid email address",
            "VALIDATION_ERROR",
            {"email": ["Please enter a valid email address"]},
        )
    if not _CODE_RE.match(raw_code):
        raise APIError(
            422,
            "Please enter the 6-digit code from your email",
            "VALIDATION_ERROR",
            {"code": ["Please enter the 6-digit code from your email"]},
        )
    pwd = password or ""
    if len(pwd) < _MIN_PASSWORD_LEN or len(pwd) > _MAX_PASSWORD_LEN:
        raise APIError(
            422,
            f"Password must be {_MIN_PASSWORD_LEN}–{_MAX_PASSWORD_LEN} characters",
            "VALIDATION_ERROR",
            {"password": [f"Password must be {_MIN_PASSWORD_LEN}–{_MAX_PASSWORD_LEN} characters"]},
        )

    normalized = _normalize_email(raw_email)
    user = await _find_active_user_by_email(db, normalized)
    if user is None:
        raise APIError(404, "No account found with this email address.", "NOT_FOUND")

    employee_id = str(user.get("employee_id") or "")
    rows = await db.select(
        "password_reset_requests",
        filters={"employee_id": employee_id},
        order="created_at.desc",
        limit=5,
    )
    now = datetime.now(timezone.utc)
    expected_hash = _hash_code(normalized, raw_code)
    match_row = None
    for row in rows:
        if row.get("used_at"):
            continue
        expires_raw = row.get("expires_at")
        try:
            expires = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if expires < now:
            continue
        if str(row.get("code_hash") or "") == expected_hash:
            match_row = row
            break

    if match_row is None:
        raise APIError(
            400,
            "Invalid or expired reset code.",
            "INVALID_CODE",
            {"code": ["Invalid or expired reset code."]},
        )

    await db.update("users", {"password": pwd}, {"employee_id": employee_id})
    row_id = match_row.get("id")
    if row_id is not None:
        await db.update(
            "password_reset_requests",
            {"used_at": now.isoformat()},
            {"id": row_id},
        )
    await _invalidate_pending(db, employee_id)
