"""Transactional email — SendGrid, Resend, or SMTP."""

from __future__ import annotations

import logging
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from app.config import (
    RESEND_API_KEY,
    RESEND_FROM,
    SENDGRID_API_KEY,
    SENDGRID_FROM,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_TLS,
    SMTP_USER,
)

logger = logging.getLogger(__name__)

DEFAULT_FROM = "CMS <onboarding@resend.dev>"


def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def resend_configured() -> bool:
    return bool((RESEND_API_KEY or "").strip())


def sendgrid_configured() -> bool:
    return bool((SENDGRID_API_KEY or "").strip() and _from_raw().email)


def email_delivery_configured() -> bool:
    return sendgrid_configured() or smtp_configured() or resend_configured()


class _ParsedFrom:
    def __init__(self, email: str, name: str) -> None:
        self.email = email
        self.name = name


def _from_raw() -> _ParsedFrom:
    raw = (SENDGRID_FROM or RESEND_FROM or SMTP_FROM or DEFAULT_FROM).strip()
    match = re.match(r"^(.*)<([^>]+)>$", raw)
    if match:
        name = match.group(1).strip().strip('"') or "CMS"
        return _ParsedFrom(email=match.group(2).strip(), name=name)
    if "@" in raw:
        return _ParsedFrom(email=raw, name="CMS")
    return _ParsedFrom(email="", name="CMS")


def _from_header() -> str:
    parsed = _from_raw()
    if not parsed.email:
        return DEFAULT_FROM
    if parsed.name:
        return f"{parsed.name} <{parsed.email}>"
    return parsed.email


def send_via_sendgrid_sync(to: str, subject: str, text_body: str, html_body: str | None) -> None:
    if not SENDGRID_API_KEY:
        raise RuntimeError("SendGrid is not configured")
    sender = _from_raw()
    if not sender.email:
        raise RuntimeError("SENDGRID_FROM is not set")
    payload: dict = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": sender.email, "name": sender.name},
        "subject": subject,
        "content": [{"type": "text/plain", "value": text_body}],
    }
    if html_body:
        payload["content"].append({"type": "text/html", "value": html_body})
    response = httpx.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30.0,
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        logger.error("SendGrid failed %s: %s", response.status_code, detail)
        try:
            errors = response.json().get("errors")
            if isinstance(errors, list) and errors:
                message = errors[0].get("message") if isinstance(errors[0], dict) else None
            else:
                message = None
        except Exception:
            message = None
        raise RuntimeError(message or detail or "Could not send reset email")


def send_via_resend_sync(to: str, subject: str, text_body: str, html_body: str | None) -> None:
    from_addr = _from_header()
    if not RESEND_API_KEY or not from_addr:
        raise RuntimeError("Resend is not configured")
    payload = {
        "from": from_addr,
        "to": [to],
        "subject": subject,
        "text": text_body,
    }
    if html_body:
        payload["html"] = html_body
    response = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30.0,
    )
    if response.status_code >= 400:
        detail = response.text[:400]
        logger.error("Resend failed %s: %s", response.status_code, detail)
        try:
            err = response.json()
            message = err.get("message") if isinstance(err, dict) else None
        except Exception:
            message = None
        raise RuntimeError(message or "Could not send reset email")


def send_email_sync(to: str, subject: str, text_body: str, html_body: str | None = None) -> None:
    if not smtp_configured():
        raise RuntimeError("SMTP is not configured")
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = SMTP_FROM
    message["To"] = to
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        message.attach(MIMEText(html_body, "html", "utf-8"))

    port = SMTP_PORT
    if SMTP_USE_TLS:
        with smtplib.SMTP(SMTP_HOST, port, timeout=30) as client:
            client.ehlo()
            client.starttls()
            client.ehlo()
            if SMTP_USER and SMTP_PASSWORD:
                client.login(SMTP_USER, SMTP_PASSWORD)
            client.sendmail(SMTP_FROM, [to], message.as_string())
    else:
        with smtplib.SMTP_SSL(SMTP_HOST, port, timeout=30) as client:
            if SMTP_USER and SMTP_PASSWORD:
                client.login(SMTP_USER, SMTP_PASSWORD)
            client.sendmail(SMTP_FROM, [to], message.as_string())


def send_password_reset_email(to: str, user_name: str, code: str, minutes_valid: int) -> None:
    subject = "CMS password reset code"
    greeting = user_name.strip() or "there"
    text = (
        f"Hello {greeting},\n\n"
        f"You requested a password reset for your CMS account.\n\n"
        f"Your reset code is: {code}\n"
        f"This code expires in {minutes_valid} minutes.\n\n"
        f"Open the CMS app → Forgot Password → enter this code with your new password.\n\n"
        f"If you did not request this, you can ignore this email.\n"
    )
    html = (
        f"<p>Hello {greeting},</p>"
        f"<p>You requested a password reset for your <strong>CMS</strong> account.</p>"
        f"<p style='font-size:22px;font-weight:bold;letter-spacing:4px'>{code}</p>"
        f"<p>This code expires in {minutes_valid} minutes.</p>"
        f"<p>Open the CMS app, go to <em>Forgot Password</em>, and enter this code with your new password.</p>"
        f"<p>If you did not request this, you can ignore this email.</p>"
    )
    if sendgrid_configured():
        send_via_sendgrid_sync(to, subject, text, html)
    elif resend_configured():
        send_via_resend_sync(to, subject, text, html)
    else:
        send_email_sync(to, subject, text, html)
