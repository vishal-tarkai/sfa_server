"""In-app notifications + optional FCM dispatch via Supabase Edge Function."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from app.config import SUPABASE_ANON_KEY, SUPABASE_URL
from app.db import DB
from app.util import new_id, now_iso

logger = logging.getLogger(__name__)


def _functions_base_url() -> str:
    explicit = os.environ.get("SUPABASE_FUNCTIONS_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    if SUPABASE_URL:
        return f"{SUPABASE_URL.rstrip('/')}/functions/v1"
    return ""


def _fcm_webhook_secret() -> str:
    return os.environ.get("FCM_WEBHOOK_SECRET", "").strip()


async def dispatch_fcm_push(
    *,
    employee_id: str,
    title: str,
    body: str,
    notification_id: str = "",
    reference_type: str = "",
    reference_id: str = "",
) -> int:
    """Call `send-fcm-notification` edge function. Returns count sent (0 if skipped)."""
    secret = _fcm_webhook_secret()
    base = _functions_base_url()
    if not secret or not base or not employee_id:
        return 0

    url = f"{base}/send-fcm-notification"
    headers = {
        "Content-Type": "application/json",
        "x-webhook-secret": secret,
    }
    if SUPABASE_ANON_KEY:
        headers["apikey"] = SUPABASE_ANON_KEY

    payload = {
        "employee_id": employee_id,
        "title": title,
        "body": body,
        "notification_id": notification_id,
        "reference_type": reference_type,
        "reference_id": reference_id,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            logger.warning(
                "FCM edge function HTTP %s: %s",
                response.status_code,
                response.text[:500],
            )
            return 0
        data = response.json()
        return int(data.get("sent") or 0)
    except Exception as exc:
        logger.warning("FCM dispatch failed: %s", exc)
        return 0


async def create_user_notification(
    db: DB,
    *,
    employee_id: str,
    title: str,
    description: str,
    notification_type: str = "report",
    reference_type: str | None = None,
    reference_id: str | None = None,
    action_url: str | None = None,
    action_label: str | None = None,
    notification_id: str | None = None,
    push: bool = True,
) -> dict[str, Any]:
    """Insert `user_notifications` and optionally dispatch FCM from the API."""
    nid = (notification_id or "").strip() or new_id("NTF")
    row = {
        "notification_id": nid,
        "employee_id": employee_id,
        "title": title,
        "description": description,
        "type": notification_type,
        "is_read": False,
        "action_label": action_label,
        "action_url": action_url or "/notifications",
        "has_action_buttons": False,
        "reference_type": reference_type,
        "reference_id": reference_id,
        "created_at": now_iso(),
    }

    existing = await db.select(
        "user_notifications", filters={"notification_id": nid}, limit=1
    )
    if existing:
        return existing[0]

    await db.insert("user_notifications", row)

    if push and _fcm_webhook_secret():
        await dispatch_fcm_push(
            employee_id=employee_id,
            title=title,
            body=description,
            notification_id=nid,
            reference_type=reference_type or "",
            reference_id=reference_id or "",
        )

    return row


def _safe_notification_id(prefix: str, raw_id: str) -> str:
    return f"{prefix}_{raw_id.replace('-', '_')}"


async def notify_dpr_submitted_to_manager(
    db: DB,
    *,
    submitter_id: str,
    submitter_name: str,
    manager_employee_id: str,
    dpr_id: str,
    report_date: str,
) -> None:
    if not manager_employee_id or manager_employee_id == submitter_id:
        return
    title = "DPR submitted for review"
    description = f"{submitter_name} submitted a DPR for {report_date}."
    await create_user_notification(
        db,
        employee_id=manager_employee_id,
        title=title,
        description=description,
        notification_type="report",
        reference_type="dpr",
        reference_id=dpr_id,
        action_url="/notifications",
        notification_id=_safe_notification_id("dpr_submit", dpr_id),
    )


async def notify_dpr_submitted_to_seniors(
    db: DB,
    *,
    submitter_id: str,
    submitter_name: str,
    senior_employee_ids: list[str],
    dpr_id: str,
    report_date: str,
) -> None:
    """Alert reporting-chain managers when a DPR is submitted or updated."""
    if not senior_employee_ids:
        return
    title = "DPR submitted for review"
    description = f"{submitter_name} submitted a DPR for {report_date}."
    for senior_id in senior_employee_ids:
        sid = str(senior_id or "").strip()
        if not sid or sid == submitter_id:
            continue
        await create_user_notification(
            db,
            employee_id=sid,
            title=title,
            description=description,
            notification_type="report",
            reference_type="dpr",
            reference_id=dpr_id,
            action_url="/notifications",
            notification_id=_safe_notification_id(f"dpr_submit_{sid}", dpr_id),
        )


async def notify_dpr_joint_working_tagged(
    db: DB,
    *,
    submitter_id: str,
    submitter_name: str,
    senior_employee_id: str,
    dpr_id: str,
    report_date: str,
    visit_count: int,
) -> None:
    if not senior_employee_id or senior_employee_id == submitter_id:
        return
    title = "Joint working DPR"
    visits = f"{visit_count} visit{'s' if visit_count != 1 else ''}"
    description = (
        f"{submitter_name} tagged you in joint working on {report_date} "
        f"({visits}). Your DPR was updated."
    )
    await create_user_notification(
        db,
        employee_id=senior_employee_id,
        title=title,
        description=description,
        notification_type="report",
        reference_type="dpr",
        reference_id=dpr_id,
        action_url="/notifications",
        notification_id=_safe_notification_id(
            f"dpr_jw_{senior_employee_id}",
            dpr_id,
        ),
    )


async def notify_tour_submitted_to_manager(
    db: DB,
    *,
    submitter_name: str,
    manager_employee_id: str,
    approval_id: str,
    plan_label: str,
) -> None:
    if not manager_employee_id:
        return
    title = "Tour plan submitted for approval"
    description = f"{submitter_name} submitted a tour plan for {plan_label}."
    await create_user_notification(
        db,
        employee_id=manager_employee_id,
        title=title,
        description=description,
        notification_type="report",
        reference_type="tour",
        reference_id=approval_id,
        action_url="/notifications",
        notification_id=_safe_notification_id("tour_submit", approval_id),
    )


async def notify_pob_submitted_to_seniors(
    db: DB,
    *,
    submitter_id: str,
    submitter_name: str,
    senior_employee_ids: list[str],
    order_id: str,
    contact_name: str,
    total_amount: float,
    item_count: int,
) -> None:
    """Alert each manager in the submitter's reporting / org chain."""
    if not senior_employee_ids:
        return
    amount_label = (
        f"₹{int(total_amount):,}"
        if float(total_amount).is_integer()
        else f"₹{total_amount:,.2f}"
    )
    contact = (contact_name or "").strip() or "customer"
    title = "New POB order"
    description = (
        f"{submitter_name} placed a POB order for {contact} "
        f"({item_count} item{'s' if item_count != 1 else ''}, {amount_label})."
    )
    for senior_id in senior_employee_ids:
        sid = (senior_id or "").strip()
        if not sid or sid == submitter_id:
            continue
        await create_user_notification(
            db,
            employee_id=sid,
            title=title,
            description=description,
            notification_type="report",
            reference_type="pob",
            reference_id=order_id,
            action_url="/notifications",
            notification_id=_safe_notification_id(f"pob_submit_{sid}", order_id),
        )


async def notify_leave_submitted_to_manager(
    db: DB,
    *,
    submitter_name: str,
    manager_employee_id: str,
    approval_id: str,
    leave_label: str,
    date_range: str,
) -> None:
    """Used when DB leave-submit trigger is not configured."""
    if not manager_employee_id:
        return
    title = "New leave request"
    description = f"{submitter_name} applied for {leave_label} ({date_range})."
    await create_user_notification(
        db,
        employee_id=manager_employee_id,
        title=title,
        description=description,
        notification_type="attendance",
        reference_type="leave",
        reference_id=approval_id,
        action_url="/leaves",
        notification_id=_safe_notification_id("leave_submit", approval_id),
    )


async def notify_dpr_reviewed_to_employee(
    db: DB,
    *,
    employee_id: str,
    dpr_id: str,
    report_date: str,
    approved: bool,
    review_note: str = "",
) -> None:
    if not employee_id:
        return
    if approved:
        title = "DPR Approved"
        description = f"Your DPR for {report_date} has been approved."
        suffix = "approved"
    else:
        title = "DPR Rejected"
        description = f"Your DPR for {report_date} has been rejected."
        if review_note.strip():
            description = f"{description} Remarks: {review_note.strip()}"
        suffix = "rejected"
    await create_user_notification(
        db,
        employee_id=employee_id,
        title=title,
        description=description,
        notification_type="report",
        reference_type="dpr",
        reference_id=dpr_id,
        action_url="/notifications",
        notification_id=_safe_notification_id(f"dpr_{suffix}", dpr_id),
    )


async def notify_leave_reviewed_to_employee(
    db: DB,
    *,
    employee_id: str,
    approval_id: str,
    leave_label: str,
    date_range: str,
    approved: bool,
    review_note: str = "",
) -> None:
    if not employee_id:
        return
    if approved:
        title = "Leave Approved"
        description = f"Your {leave_label} for {date_range} has been approved."
        suffix = "approved"
    else:
        title = "Leave Rejected"
        description = f"Your {leave_label} for {date_range} has been rejected."
        if review_note.strip():
            description = f"{description} Remarks: {review_note.strip()}"
        suffix = "rejected"
    await create_user_notification(
        db,
        employee_id=employee_id,
        title=title,
        description=description,
        notification_type="attendance",
        reference_type="leave",
        reference_id=approval_id,
        action_url="/leaves",
        notification_id=_safe_notification_id(f"leave_{suffix}", approval_id),
    )


async def notify_expense_submitted_to_seniors(
    db: DB,
    *,
    submitter_id: str,
    submitter_name: str,
    senior_employee_ids: list[str],
    claim_id: str,
    claim_date: str,
    total_amount: float,
) -> None:
    """Alert managers in the submitter's hierarchy that a claim needs review."""
    if not senior_employee_ids:
        return
    amount_label = (
        f"₹{int(total_amount):,}"
        if float(total_amount).is_integer()
        else f"₹{total_amount:,.2f}"
    )
    title = "New expense claim"
    description = (
        f"{submitter_name} submitted an expense claim for {claim_date} "
        f"({amount_label})."
    )
    for senior_id in senior_employee_ids:
        sid = str(senior_id or "").strip()
        if not sid or sid == submitter_id:
            continue
        await create_user_notification(
            db,
            employee_id=sid,
            title=title,
            description=description,
            notification_type="report",
            reference_type="expense",
            reference_id=claim_id,
            action_url="/expenses",
            notification_id=_safe_notification_id(
                f"expense_submit_{sid}",
                claim_id,
            ),
        )


async def notify_expense_reviewed_to_employee(
    db: DB,
    *,
    employee_id: str,
    claim_id: str,
    claim_date: str,
    approved: bool,
    review_note: str = "",
) -> None:
    if not employee_id:
        return
    if approved:
        title = "Expense claim approved"
        description = f"Your expense claim for {claim_date} has been approved."
        suffix = "approved"
    else:
        title = "Expense claim rejected"
        description = f"Your expense claim for {claim_date} has been rejected."
        if review_note.strip():
            description = f"{description} Remarks: {review_note.strip()}"
        suffix = "rejected"
    await create_user_notification(
        db,
        employee_id=employee_id,
        title=title,
        description=description,
        notification_type="report",
        reference_type="expense",
        reference_id=claim_id,
        action_url="/expenses",
        notification_id=_safe_notification_id(f"expense_{suffix}", claim_id),
    )


async def notify_tour_reviewed_to_employee(
    db: DB,
    *,
    employee_id: str,
    approval_id: str,
    plan_label: str,
    approved: bool,
    review_note: str = "",
) -> None:
    if not employee_id:
        return
    if approved:
        title = "Tour plan approved"
        description = f"Your tour plan for {plan_label} has been approved."
        suffix = "approved"
    else:
        title = "Tour plan rejected"
        description = f"Your tour plan for {plan_label} has been rejected."
        if review_note.strip():
            description = f"{description} Remarks: {review_note.strip()}"
        suffix = "rejected"
    await create_user_notification(
        db,
        employee_id=employee_id,
        title=title,
        description=description,
        notification_type="report",
        reference_type="tour",
        reference_id=approval_id,
        action_url="/notifications",
        notification_id=_safe_notification_id(f"tour_{suffix}", approval_id),
    )
