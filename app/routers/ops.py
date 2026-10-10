"""Admin Head Offices, broadcasts, audit, and single-record lookups."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends

from app.auth import admin_user
from app.db import DB
from app.deps import get_db
from app.errors import APIError, respond
from app.routers.field import _offices
from app.util import entity_card, new_id, now_iso, num, product_card, public_user

router = APIRouter(prefix="/admin", tags=["admin"])


def _office_card(row: dict) -> dict:
    return {
        "id": row.get("ho_id") or "",
        "ho_id": row.get("ho_id") or "",
        "name": row.get("name") or "Head Office",
        "address": row.get("address") or "",
        "latitude": num(row.get("latitude")),
        "longitude": num(row.get("longitude")),
        "radius_m": num(row.get("radius_meters")) or 200,
        "radius_meters": num(row.get("radius_meters")) or 200,
        "status": row.get("status") or "active",
        "updated_at": row.get("updated_at"),
    }


def _broadcast_card(row: dict) -> dict:
    return {
        "id": row.get("notification_id") or "",
        "title": row.get("title") or "",
        "description": row.get("description") or "",
        "category": row.get("category") or "General",
        "audience": row.get("audience") or "All Users",
        "territory": row.get("territory") or "",
        "role": row.get("role") or "",
        "sent_by": row.get("sent_by") or "Admin",
        "recipient_count": int(num(row.get("recipient_count"))),
        "created_at": row.get("created_at"),
    }


def _audit_card(row: dict) -> dict:
    return {
        "id": row.get("audit_id") or row.get("id"),
        "timestamp": row.get("created_at"),
        "actor": row.get("actor") or "",
        "module": row.get("module") or "",
        "action": row.get("action") or "",
        "entity": row.get("entity") or "",
        "details": row.get("details") or "",
    }


async def write_audit(
    db: DB,
    *,
    actor: str,
    module: str,
    action: str,
    entity: str,
    details: str = "",
) -> None:
    try:
        await db.insert("audit_logs", {
            "audit_id": new_id("AUD"),
            "actor": actor,
            "module": module,
            "action": action,
            "entity": entity,
            "details": details,
            "created_at": now_iso(),
        })
    except APIError:
        return


# --- head offices ---------------------------------------------------------

@router.get("/head-offices")
async def list_head_offices(user: dict = Depends(admin_user), db: DB = Depends(get_db), status: str = ""):
    del user
    try:
        rows = await db.select("head_offices", order="name.asc")
    except APIError:
        return respond("OK", {"items": await _offices(db)})
    items = [_office_card(row) for row in rows if not status or row.get("status") == status]
    return respond("OK", {"items": items})


@router.get("/head-offices/{ho_id}")
async def get_head_office(ho_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("head_offices", filters={"ho_id": ho_id}, limit=1)
    if not rows:
        raise APIError(404, "Head office not found", "NOT_FOUND")
    return respond("OK", _office_card(rows[0]))


@router.post("/head-offices")
async def create_head_office(body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    name = str(body.get("name") or "").strip()
    if not name:
        raise APIError(422, "Name is required", "VALIDATION_ERROR", {"name": ["Name is required"]})
    ho_id = body.get("ho_id") or body.get("id") or new_id("HO")
    row = {
        "ho_id": ho_id,
        "name": name,
        "address": body.get("address") or "",
        "latitude": num(body.get("latitude")),
        "longitude": num(body.get("longitude")),
        "radius_meters": num(body.get("radius_meters") or body.get("radius_m")) or 200,
        "status": body.get("status") or "active",
        "updated_at": now_iso(),
    }
    saved = await db.insert("head_offices", row)
    await write_audit(db, actor=user.get("email") or "Admin", module="Head Office", action="CREATE", entity=ho_id, details=name)
    return respond("Head office created", _office_card(saved[0] if saved else row), 201)


@router.patch("/head-offices/{ho_id}")
async def update_head_office(ho_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    allowed = {
        "name": "name",
        "address": "address",
        "latitude": "latitude",
        "longitude": "longitude",
        "status": "status",
        "radius_meters": "radius_meters",
        "radius_m": "radius_meters",
    }
    updates = {}
    for key, column in allowed.items():
        if key not in body:
            continue
        value = body[key]
        updates[column] = num(value) if column in ("latitude", "longitude", "radius_meters") else value
    updates["updated_at"] = now_iso()
    saved = await db.update("head_offices", updates, {"ho_id": ho_id})
    if not saved:
        raise APIError(404, "Head office not found", "NOT_FOUND")
    await write_audit(db, actor=user.get("email") or "Admin", module="Head Office", action="UPDATE", entity=ho_id, details=saved[0].get("name") or "")
    return respond("Head office updated", _office_card(saved[0]))


@router.patch("/head-offices/{ho_id}/status")
async def head_office_status(ho_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    status = body.get("status") or "active"
    if status not in ("active", "inactive"):
        raise APIError(422, "Status must be active or inactive", "VALIDATION_ERROR")
    saved = await db.update("head_offices", {"status": status, "updated_at": now_iso()}, {"ho_id": ho_id})
    if not saved:
        raise APIError(404, "Head office not found", "NOT_FOUND")
    await write_audit(db, actor=user.get("email") or "Admin", module="Head Office", action="UPDATE", entity=ho_id, details=status)
    return respond("Status updated", {"ho_id": ho_id, "status": status})


@router.delete("/head-offices/{ho_id}")
async def delete_head_office(ho_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    await db.delete("head_offices", {"ho_id": ho_id})
    await write_audit(db, actor=user.get("email") or "Admin", module="Head Office", action="DELETE", entity=ho_id)
    return respond("Head office deleted", None)


# --- lookups --------------------------------------------------------------

@router.get("/users/{employee_id}")
async def get_user(employee_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("users", filters={"employee_id": employee_id}, limit=1)
    if not rows:
        raise APIError(404, "User not found", "NOT_FOUND")
    return respond("OK", public_user(rows[0]))


@router.get("/products/{product_id}")
async def get_product(product_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("products", filters={"product_id": product_id}, limit=1)
    if not rows:
        raise APIError(404, "Product not found", "NOT_FOUND")
    return respond("OK", product_card(rows[0]))


@router.get("/healthcare/{entity_id}")
async def get_healthcare(entity_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("healthcare_entities", filters={"entity_id": entity_id}, limit=1)
    if not rows:
        raise APIError(404, "Entity not found", "NOT_FOUND")
    stockist = ""
    if rows[0].get("stockist_id"):
        linked = await db.select("healthcare_entities", filters={"entity_id": rows[0]["stockist_id"]}, limit=1)
        stockist = linked[0].get("name") if linked else ""
    return respond("OK", entity_card(rows[0], stockist))


# --- broadcasts -----------------------------------------------------------

async def _audience_users(db: DB, body: dict) -> list[dict]:
    audience = str(body.get("audience") or "All Users")
    rows = await db.select("users", filters={"status": "active"})
    wanted = audience.strip().lower()
    if wanted in ("all users", "all", ""):
        return rows
    if wanted in ("all mrs", "mr"):
        return [row for row in rows if "mr" in str(row.get("designation") or row.get("role") or "").lower()]
    territory = str(body.get("territory") or "").strip().lower()
    role = str(body.get("role") or "").strip().lower()
    if wanted in ("by territory", "territory") or territory:
        return [row for row in rows if (row.get("territory") or "").strip().lower() == territory]
    if wanted in ("by role", "role") or role:
        return [
            row for row in rows
            if (row.get("role") or "").strip().lower() == role
            or (row.get("designation") or "").strip().lower() == role
        ]
    return rows


@router.get("/notifications")
async def list_broadcasts(user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    try:
        rows = await db.select("admin_notifications", order="created_at.desc")
    except APIError:
        rows = []
    return respond("OK", {"items": [_broadcast_card(row) for row in rows]})


@router.post("/notifications")
async def send_broadcast(body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    title = str(body.get("title") or "").strip()
    description = str(body.get("description") or body.get("message") or "").strip()
    if not title or not description:
        raise APIError(422, "Title and message are required", "VALIDATION_ERROR")
    recipients = await _audience_users(db, body)
    notification_id = new_id("N")
    category = body.get("category") or "General"
    audience = body.get("audience") or "All Users"
    note_type = {
        "Target": "order",
        "Product": "inventory",
        "Compliance": "attendance",
        "HR": "report",
        "Policy": "report",
    }.get(category, "report")
    row = {
        "notification_id": notification_id,
        "title": title,
        "description": description,
        "category": category,
        "audience": audience,
        "territory": body.get("territory") or "",
        "role": body.get("role") or "",
        "sent_by": user.get("email") or "Admin",
        "recipient_count": len(recipients),
        "created_at": now_iso(),
    }
    try:
        await db.insert("admin_notifications", row)
    except APIError:
        pass
    copies = [
        {
            "notification_id": f"{notification_id}-{person.get('employee_id')}",
            "employee_id": person.get("employee_id"),
            "title": title,
            "description": description,
            "type": note_type,
            "is_read": False,
            "action_label": body.get("action_label") or "View",
            "action_url": body.get("action_url") or "/notifications",
            "created_at": now_iso(),
        }
        for person in recipients
        if person.get("employee_id")
    ]
    if copies:
        try:
            await db.insert("user_notifications", copies)
        except APIError:
            for copy in copies:
                try:
                    await db.insert("user_notifications", copy)
                except APIError:
                    continue
    await write_audit(
        db,
        actor=user.get("email") or "Admin",
        module="Notifications",
        action="SEND",
        entity=notification_id,
        details=f"{title} → {audience} ({len(recipients)})",
    )
    return respond("Notification sent", {**_broadcast_card(row), "recipient_count": len(recipients)}, 201)


@router.delete("/notifications/{notification_id}")
async def delete_broadcast(notification_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    try:
        await db.delete("admin_notifications", {"notification_id": notification_id})
    except APIError:
        pass
    try:
        copies = await db.select("user_notifications")
        for row in copies:
            if str(row.get("notification_id") or "").startswith(f"{notification_id}-"):
                await db.delete("user_notifications", {"notification_id": row.get("notification_id")})
    except APIError:
        pass
    await write_audit(db, actor=user.get("email") or "Admin", module="Notifications", action="DELETE", entity=notification_id)
    return respond("Notification deleted", None)


# --- audit ----------------------------------------------------------------

@router.get("/audit")
async def list_audit(
    user: dict = Depends(admin_user),
    db: DB = Depends(get_db),
    module: str = "",
    action: str = "",
    actor: str = "",
    q: str = "",
    page: int = 1,
    limit: int = 50,
):
    del user
    try:
        rows = await db.select("audit_logs", order="created_at.desc")
    except APIError:
        rows = []
    items = []
    for row in rows:
        card = _audit_card(row)
        if module and card["module"].lower() != module.lower():
            continue
        if action and card["action"].lower() != action.lower():
            continue
        if actor and actor.lower() not in card["actor"].lower():
            continue
        if q:
            blob = f"{card['entity']} {card['details']} {card['actor']}".lower()
            if q.lower() not in blob:
                continue
        items.append(card)
    limit = min(100, max(1, limit or 50))
    page = max(1, page or 1)
    start = (page - 1) * limit
    return respond("OK", {
        "items": items[start:start + limit],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": len(items),
            "total_pages": max(1, (len(items) + limit - 1) // limit) if items else 1,
        },
    })
