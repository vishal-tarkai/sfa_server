"""Transactional email (SMTP). Configure via SMTP_* env vars."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import (
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_TLS,
    SMTP_USER,
)

logger = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


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
    send_email_sync(to, subject, text, html)
