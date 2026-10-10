"""Admin routes. Paths match API.md Part B."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Body, Depends, File, Query, UploadFile

from app.auth import admin_user, drop_token, issue_token
from app.config import ADMIN_EMAIL, ADMIN_PASSWORD, FIELD_DEFAULT_PASSWORD
from app.db import DB
from app.deps import get_db
from app.errors import APIError, respond
from app.routers.field import (
    _balance,
    _claim,
    _dpr_detail,
    _dpr_list_item,
    _file_card,
    _folder_card,
    _folder_tree,
    _folders,
    _leave_item,
    _name_map,
    _order_summary,
    _review_leave,
    _review_tour,
    _set_dpr_status,
    _tour_queue_item,
    _tour_view,
)
from app.util import (
    as_date,
    entity_card,
    in_month,
    month_bounds,
    new_id,
    now_iso,
    num,
    paginate,
    period_label,
    product_card,
    public_user,
    today_str,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/auth/login")
async def admin_login(
    body: dict = Body(
        ...,
        openapi_examples={
            "admin": {
                "summary": "Admin login",
                "value": {
                    "email": "vishal@tarkai.com",
                    "password": "1234",
                },
            }
        },
    ),
    db: DB = Depends(get_db),
):
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    if not email or not password:
        raise APIError(422, "Email and password are required", "VALIDATION_ERROR")
    if email != ADMIN_EMAIL or password != ADMIN_PASSWORD:
        raise APIError(401, "Invalid email or password.", "INVALID_CREDENTIALS")
    admin = {"email": ADMIN_EMAIL, "full_name": "Vishal", "role": "super_admin", "is_active": True, "last_login_at": now_iso()}
    try:
        rows = await db.select("admin_logins", filters={"email": email}, limit=1)
        if rows:
            if rows[0].get("is_active") is False:
                raise APIError(403, "This account is disabled", "ACCOUNT_DISABLED")
            admin = {
                "email": rows[0].get("email") or email,
                "full_name": rows[0].get("full_name") or "Admin",
                "role": rows[0].get("role") or "super_admin",
                "is_active": True,
                "last_login_at": now_iso(),
            }
            await db.update("admin_logins", {"last_login_at": now_iso()}, {"email": email})
    except APIError as exc:
        if exc.code == "ACCOUNT_DISABLED":
            raise
    token, expires = issue_token("admin", email=admin["email"])
    return respond("Login successful", {"access_token": token, "expires_in": expires, "admin": admin})


@router.post("/auth/logout")
async def admin_logout(user: dict = Depends(admin_user)):
    drop_token(user.get("token"))
    return respond("Logged out", None)


@router.get("/dashboard")
async def dashboard(user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    users = await db.select("users")
    active = [row for row in users if row.get("status") == "active"]
    month = date.today().strftime("%Y-%m")
    added = 0
    for row in active:
        joined = str(row.get("join_date") or row.get("created_at") or "")
        if month in joined or joined.startswith(month):
            added += 1
    reports = await db.select("dpr_reports")
    start = (date.today() - timedelta(days=14)).isoformat()
    recent = [row for row in reports if as_date(row.get("report_date")) >= start]
    working_days = 10
    expected = max(1, len(active) * working_days)
    compliance = round(len(recent) * 100 / expected) if active else 0
    period = period_label()
    targets = await db.select("sales_targets", filters={"period": period})
    target_sum = sum(num(row.get("target_amount")) for row in targets)
    orders = await db.select("pob_orders")
    start_m, end_m = month_bounds(date.today().strftime("%Y-%m"))
    achieved = sum(num(row.get("total_amount")) for row in orders if row.get("status") in ("submitted", "approved") and in_month(row.get("created_at"), start_m, end_m))
    sales_pct = round(achieved * 100 / target_sum) if target_sum else 0
    pending_dpr = sum(1 for row in reports if row.get("status") == "pending")
    approvals = await db.select("approval_requests", filters={"status": "pending"})
    attendance = await db.select("attendance_records", filters={"review_status": "pending"})
    expenses = await db.select("expense_claims", filters={"status": "pending"})
    pending = pending_dpr + len(approvals) + len(attendance) + len(expenses)
    trend = []
    anchor = date.today()
    for offset in range(5, -1, -1):
        month_index = anchor.month - offset
        year = anchor.year
        while month_index <= 0:
            month_index += 12
            year -= 1
        label = period_label(date(year, month_index, 1))
        bounds = month_bounds(f"{year}-{month_index:02d}")
        trend.append({
            "month": label,
            "target_inr": int(sum(num(row.get("target_amount")) for row in await db.select("sales_targets", filters={"period": label}))),
            "achieved_inr": int(sum(num(row.get("total_amount")) for row in orders if row.get("status") in ("submitted", "approved") and in_month(row.get("created_at"), bounds[0], bounds[1]))),
        })
    today_orders = sum(1 for row in orders if as_date(row.get("created_at")) == today_str())
    return respond("OK", {
        "active_users": len(active),
        "active_users_added_this_month": added,
        "pending_approvals": pending,
        "high_priority_approvals": pending_dpr,
        "dpr_compliance_pct": min(compliance, 100),
        "dpr_compliance_delta_pct": 0,
        "sales_achievement_pct": sales_pct,
        "sales_achievement_delta_pct": 0,
        "sales_trend_label": f"{trend[0]['month'].split()[0][:3]}–{trend[-1]['month']}" if trend else "",
        "sales_trend": trend,
        "today_order_count": today_orders,
    })


@router.get("/dashboard/orders")
async def recent_orders(user: dict = Depends(admin_user), db: DB = Depends(get_db), limit: int = 20):
    del user
    rows = await db.select("pob_orders", order="created_at.desc", limit=limit)
    names = await _name_map(db)
    items = []
    for row in rows:
        person = names.get(row.get("employee_id")) or {}
        items.append(_order_summary(row, person.get("name") or "", person.get("territory") or ""))
    return respond("OK", {"items": items})


@router.get("/nav-badges")
async def nav_badges(user: dict = Depends(admin_user), db: DB = Depends(get_db), pob_since: str = ""):
    del user
    dpr = await db.select("dpr_reports", filters={"status": "pending"})
    tours = await db.select("approval_requests", filters={"request_type": "tour", "status": "pending"})
    leaves = await db.select("approval_requests", filters={"request_type": "leave", "status": "pending"})
    attendance = await db.select("attendance_records", filters={"review_status": "pending"})
    expenses = await db.select("expense_claims", filters={"status": "pending"})
    orders = await db.select("pob_orders", order="created_at.desc")
    since = pob_since or f"{today_str()}T00:00:00Z"
    new_orders = sum(1 for row in orders if str(row.get("created_at") or "") >= since)
    return respond("OK", {
        "pending_approvals": len(dpr) + len(tours) + len(leaves) + len(attendance) + len(expenses),
        "new_pob_orders": new_orders,
        "pending_dpr": len(dpr),
        "pending_tour": len(tours),
        "pending_leave": len(leaves),
        "pending_attendance": len(attendance),
        "pending_expenses": len(expenses),
    })


@router.get("/users")
async def list_users(user: dict = Depends(admin_user), db: DB = Depends(get_db), status: str = "", q: str = ""):
    del user
    rows = await db.select("users", order="name.asc")
    items = []
    for row in rows:
        if status and row.get("status") != status:
            continue
        if q and q.lower() not in (row.get("name") or "").lower() and q.lower() not in (row.get("employee_id") or "").lower():
            continue
        items.append(public_user(row))
    return respond("OK", {"items": items})


@router.post("/users")
async def create_user(body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    employee_id = body.get("employee_id") or new_id("EMP")
    row = {
        "employee_id": employee_id,
        "name": body.get("name") or "",
        "avatar": body.get("avatar") or "",
        "role": body.get("role") or "",
        "designation": body.get("designation") or "",
        "territory": body.get("territory") or "",
        "org_node_id": body.get("org_node_id"),
        "mobile": body.get("mobile") or "",
        "email": body.get("email") or "",
        "manager": body.get("manager") or "",
        "join_date": body.get("join_date") or today_str(),
        "status": body.get("status") or "active",
        "password": FIELD_DEFAULT_PASSWORD,
        "profile_image_url": body.get("profile_image_url") or "",
    }
    saved = await db.insert("users", row)
    return respond("User created", public_user(saved[0] if saved else row), 201)


@router.post("/users/reset-passwords")
async def reset_passwords(user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("users", select="employee_id")
    for row in rows:
        await db.update("users", {"password": FIELD_DEFAULT_PASSWORD}, {"employee_id": row.get("employee_id")})
    return respond("All field passwords reset", {"password": FIELD_DEFAULT_PASSWORD, "updated": True})


@router.patch("/users/{employee_id}")
async def update_user(employee_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    allowed = ("name", "avatar", "role", "designation", "territory", "org_node_id", "mobile", "email", "manager", "join_date", "status", "profile_image_url")
    updates = {key: body[key] for key in allowed if key in body}
    updates["updated_at"] = now_iso()
    saved = await db.update("users", updates, {"employee_id": employee_id})
    if not saved:
        raise APIError(404, "User not found", "NOT_FOUND")
    return respond("User updated", public_user(saved[0]))


@router.patch("/users/{employee_id}/password")
async def set_password(employee_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    password = str(body.get("password") or "")
    if not password:
        raise APIError(422, "Password is required", "VALIDATION_ERROR")
    await db.update("users", {"password": password}, {"employee_id": employee_id})
    return respond("Password updated", {"employee_id": employee_id})


@router.patch("/users/{employee_id}/status")
async def user_status(employee_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    status = body.get("status") or "active"
    if status not in ("active", "inactive", "onleave"):
        raise APIError(422, "Status must be active, inactive, or onleave", "VALIDATION_ERROR")
    await db.update("users", {"status": status}, {"employee_id": employee_id})
    return respond("Status updated", {"employee_id": employee_id, "status": status})


def _node(row: dict, users: list[dict], entities: list[dict]) -> dict:
    return {
        "node_id": row.get("node_id"),
        "label": row.get("label") or "",
        "node_type": row.get("node_type") or "",
        "parent_id": row.get("parent_id"),
        "manager_employee_id": row.get("manager_employee_id"),
        "manager_name": next((user.get("name") for user in users if user.get("employee_id") == row.get("manager_employee_id")), "—"),
        "user_count": sum(1 for user in users if user.get("org_node_id") == row.get("node_id") or user.get("territory") == row.get("label")),
        "entity_count": sum(1 for entity in entities if entity.get("territory") == row.get("label")),
        "children": [],
    }


@router.get("/organization")
async def org_tree(user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    nodes = await db.select("org_nodes", order="label.asc")
    users = await db.select("users")
    entities = await db.select("healthcare_entities")
    built = {row.get("node_id"): _node(row, users, entities) for row in nodes}
    roots = []
    for row in nodes:
        node = built[row.get("node_id")]
        parent = row.get("parent_id")
        if parent and parent in built:
            built[parent]["children"].append(node)
        else:
            roots.append(node)
    return respond("OK", {"items": roots})


@router.post("/organization/nodes")
async def create_node(body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    node_id = body.get("node_id") or new_id("org")
    row = {
        "node_id": node_id,
        "label": (body.get("label") or "").strip(),
        "node_type": body.get("node_type") or body.get("type") or "area",
        "parent_id": body.get("parent_id"),
        "manager_employee_id": body.get("manager_employee_id"),
    }
    await db.insert("org_nodes", row)
    return respond("Node created", {**row, "user_count": 0, "entity_count": 0}, 201)


@router.patch("/organization/nodes/{node_id}")
async def update_node(node_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    updates = {
        "label": (body.get("label") or "").strip(),
        "node_type": body.get("node_type") or body.get("type"),
        "manager_employee_id": body.get("manager_employee_id"),
        "updated_at": now_iso(),
    }
    updates = {key: value for key, value in updates.items() if value is not None and value != ""}
    saved = await db.update("org_nodes", updates, {"node_id": node_id})
    if not saved:
        raise APIError(404, "Node not found", "NOT_FOUND")
    return respond("Node updated", saved[0])


@router.delete("/organization/nodes/{node_id}")
async def delete_node(node_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    await db.delete("org_nodes", {"node_id": node_id})
    return respond("Node deleted", None)


@router.post("/organization/assign-user")
async def assign_user(body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    employee_id = body.get("employee_id")
    await db.update("users", {
        "org_node_id": body.get("org_node_id"),
        "territory": body.get("territory") or "",
        "updated_at": now_iso(),
    }, {"employee_id": employee_id})
    return respond("User assigned", {
        "employee_id": employee_id,
        "org_node_id": body.get("org_node_id"),
        "territory": body.get("territory") or "",
    })


@router.get("/products")
async def list_products(user: dict = Depends(admin_user), db: DB = Depends(get_db), status: str = ""):
    del user
    rows = await db.select("products", order="name.asc")
    items = [product_card(row) for row in rows if not status or row.get("status") == status]
    items.sort(key=lambda item: (not item["is_new"], item["name"]))
    return respond("OK", {"items": items})


@router.post("/products")
async def create_product(body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    product_id = body.get("product_id") or new_id("PRD")
    row = {
        "product_id": product_id,
        "name": body.get("name") or "",
        "subtitle": body.get("subtitle") or "",
        "mono_packing": body.get("mono_packing") or "",
        "shipper_qty": body.get("shipper_qty"),
        "mrp": body.get("mrp"),
        "is_new": bool(body.get("is_new")),
        "status": body.get("status") or "active",
        "image_url": body.get("image_url") or "",
        "updated_at": now_iso(),
    }
    saved = await db.insert("products", row)
    return respond("Product created", product_card(saved[0] if saved else row), 201)


@router.patch("/products/{product_id}")
async def update_product(product_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    allowed = ("name", "subtitle", "mono_packing", "shipper_qty", "mrp", "is_new", "status", "image_url")
    updates = {key: body[key] for key in allowed if key in body}
    updates["updated_at"] = now_iso()
    saved = await db.update("products", updates, {"product_id": product_id})
    if not saved:
        raise APIError(404, "Product not found", "NOT_FOUND")
    return respond("Product updated", product_card(saved[0]))


@router.patch("/products/{product_id}/status")
async def product_status(product_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    status = body.get("status") or "active"
    await db.update("products", {"status": status, "updated_at": now_iso()}, {"product_id": product_id})
    return respond("Status updated", {"product_id": product_id, "status": status})


@router.delete("/products/{product_id}")
async def delete_product(product_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    await db.delete("products", {"product_id": product_id})
    return respond("Product deleted", None)


@router.post("/products/{product_id}/image")
async def product_image(product_id: str, file: UploadFile = File(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    raw = await file.read()
    safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in (file.filename or "image.jpg"))
    path = f"{product_id}/{int(datetime.now().timestamp() * 1000)}-{safe}"
    url = await db.upload("product-images", path, raw, file.content_type or "image/jpeg")
    await db.update("products", {"image_url": url, "updated_at": now_iso()}, {"product_id": product_id})
    return respond("Image uploaded", {"product_id": product_id, "image_url": url})


@router.get("/healthcare")
async def list_healthcare(user: dict = Depends(admin_user), db: DB = Depends(get_db), type: str = "", territory: str = "", q: str = ""):
    del user
    rows = await db.select("healthcare_entities", order="name.asc")
    items = []
    for row in rows:
        if type and row.get("type") != type:
            continue
        if territory and row.get("territory") != territory:
            continue
        if q and q.lower() not in (row.get("name") or "").lower():
            continue
        items.append(entity_card(row))
    return respond("OK", {"items": items})


@router.post("/healthcare")
async def create_healthcare(body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    entity_id = body.get("entity_id") or new_id("ENT")
    row = {
        "entity_id": entity_id,
        "name": body.get("name") or "",
        "type": body.get("type") or "doctor",
        "specialty": body.get("specialty") or "",
        "territory": body.get("territory") or "",
        "city": body.get("city") or "",
        "phone": body.get("phone") or "",
        "status": body.get("status") or "active",
        "hospital": body.get("hospital") or "",
        "tier": body.get("tier") or "",
        "calls": int(num(body.get("calls"))),
        "rx": body.get("rx") or "",
        "dob": body.get("dob") or "",
        "anniversary": body.get("anniversary") or "",
        "stockist_id": body.get("stockist_id") or "",
    }
    saved = await db.insert("healthcare_entities", row)
    return respond("Entity created", entity_card(saved[0] if saved else row), 201)


@router.patch("/healthcare/{entity_id}")
async def update_healthcare(entity_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    allowed = ("name", "type", "specialty", "territory", "city", "phone", "status", "hospital", "tier", "calls", "rx", "dob", "anniversary", "stockist_id")
    updates = {key: body[key] for key in allowed if key in body}
    saved = await db.update("healthcare_entities", updates, {"entity_id": entity_id})
    if not saved:
        raise APIError(404, "Entity not found", "NOT_FOUND")
    return respond("Entity updated", entity_card(saved[0]))


@router.patch("/healthcare/{entity_id}/status")
async def healthcare_status(entity_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    status = body.get("status") or "active"
    await db.update("healthcare_entities", {"status": status}, {"entity_id": entity_id})
    return respond("Status updated", {"entity_id": entity_id, "status": status})


@router.post("/healthcare/import")
async def import_healthcare(file: UploadFile = File(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    text = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    inserted = updated = failed = 0
    errors = []
    for index, raw in enumerate(reader, start=2):
        name = (raw.get("name") or "").strip()
        if not name:
            failed += 1
            errors.append({"row": index, "message": "Missing name"})
            continue
        entity_id = (raw.get("entity_id") or "").strip() or new_id("ENT")
        row = {
            "entity_id": entity_id,
            "name": name,
            "type": (raw.get("type") or "doctor").strip(),
            "specialty": raw.get("specialty") or "",
            "territory": raw.get("territory") or "",
            "city": raw.get("city") or "",
            "phone": raw.get("phone") or "",
            "status": raw.get("status") or "active",
            "hospital": raw.get("hospital") or "",
            "tier": raw.get("tier") or "",
            "dob": raw.get("dob") or "",
            "anniversary": raw.get("anniversary") or "",
            "stockist_id": raw.get("stockist_id") or "",
        }
        found = await db.select("healthcare_entities", filters={"entity_id": entity_id}, limit=1)
        await db.upsert("healthcare_entities", row, "entity_id")
        if found:
            updated += 1
        else:
            inserted += 1
    return respond("Import finished", {"inserted": inserted, "updated": updated, "failed": failed, "errors": errors[:20]})


@router.get("/targets")
async def list_targets(user: dict = Depends(admin_user), db: DB = Depends(get_db), period: str = ""):
    del user
    period = period or period_label()
    users = await db.select("users", filters={"status": "active"}, order="name.asc")
    targets = await db.select("sales_targets", filters={"period": period})
    by_emp = {row.get("employee_id"): row for row in targets}
    orders = await db.select("pob_orders")
    try:
        name, year = period.split()
        month = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"].index(name) + 1
        start, end = month_bounds(f"{year}-{month:02d}")
    except ValueError:
        start, end = month_bounds(date.today().strftime("%Y-%m"))
    achieved_map: dict[str, float] = {}
    for row in orders:
        if row.get("status") not in ("submitted", "approved"):
            continue
        if not in_month(row.get("created_at"), start, end):
            continue
        achieved_map[row.get("employee_id")] = achieved_map.get(row.get("employee_id"), 0) + num(row.get("total_amount"))
    items = []
    for person in users:
        target = by_emp.get(person.get("employee_id")) or {}
        items.append({
            "employee_id": person.get("employee_id"),
            "employee_name": person.get("name"),
            "territory": person.get("territory") or "",
            "period": period,
            "target_amount": int(num(target.get("target_amount"))),
            "achieved_amount": int(achieved_map.get(person.get("employee_id"), 0)),
            "status": target.get("status") or "draft",
            "products": target.get("products") or [],
        })
    return respond("OK", {"period": period, "items": items})


@router.put("/targets")
async def upsert_target(body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    employee_id = body.get("employee_id")
    period = body.get("period") or period_label()
    if not employee_id:
        raise APIError(422, "employee_id is required", "VALIDATION_ERROR")
    row = {
        "employee_id": employee_id,
        "period": period,
        "target_amount": int(num(body.get("target_amount"))),
        "products": body.get("products") or [],
        "product_breakdown": body.get("product_breakdown") or [],
        "status": body.get("status") or "published",
    }
    await db.upsert("sales_targets", row, "employee_id,period")
    return respond("Target saved", {
        "employee_id": employee_id,
        "period": period,
        "target_amount": row["target_amount"],
        "status": row["status"],
    })


@router.get("/sales/achieved")
async def achieved(user: dict = Depends(admin_user), db: DB = Depends(get_db), period: str = ""):
    del user
    period = period or period_label()
    try:
        name, year = period.split()
        month = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"].index(name) + 1
        start, end = month_bounds(f"{year}-{month:02d}")
    except ValueError:
        start, end = month_bounds(date.today().strftime("%Y-%m"))
    orders = await db.select("pob_orders")
    items_rows = await db.select("pob_order_items")
    by_order: dict[str, list] = {}
    for item in items_rows:
        by_order.setdefault(item.get("order_id"), []).append(item)
    summary: dict[str, dict] = {}
    for row in orders:
        if row.get("status") not in ("submitted", "approved") or not in_month(row.get("created_at"), start, end):
            continue
        bucket = summary.setdefault(row.get("employee_id"), {"achieved_amount": 0, "order_count": 0, "products": {}})
        bucket["achieved_amount"] += num(row.get("total_amount"))
        bucket["order_count"] += 1
        for item in by_order.get(row.get("order_id"), []):
            key = item.get("product_id") or item.get("product_name")
            prod = bucket["products"].setdefault(key, {"product_id": item.get("product_id") or "", "name": item.get("product_name") or "", "quantity": 0})
            prod["quantity"] += int(num(item.get("quantity")))
    items = [
        {
            "employee_id": employee_id,
            "achieved_amount": int(bucket["achieved_amount"]),
            "order_count": bucket["order_count"],
            "products": list(bucket["products"].values()),
        }
        for employee_id, bucket in summary.items()
    ]
    return respond("OK", {"period": period, "items": items})


@router.get("/pob/orders")
async def admin_orders(user: dict = Depends(admin_user), db: DB = Depends(get_db), period: str = "", employee_id: str = ""):
    del user
    rows = await db.select("pob_orders", order="created_at.desc")
    names = await _name_map(db)
    if period:
        try:
            name, year = period.split()
            month = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"].index(name) + 1
            start, end = month_bounds(f"{year}-{month:02d}")
            rows = [row for row in rows if in_month(row.get("created_at"), start, end)]
        except ValueError:
            pass
    if employee_id:
        rows = [row for row in rows if row.get("employee_id") == employee_id]
    items = []
    for row in rows:
        person = names.get(row.get("employee_id")) or {}
        items.append(_order_summary(row, person.get("name") or "", person.get("territory") or ""))
    return respond("OK", {"items": items})


@router.get("/pob/orders/{order_id}")
async def admin_order(order_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("pob_orders", filters={"order_id": order_id}, limit=1)
    if not rows:
        raise APIError(404, "Order not found", "NOT_FOUND")
    items = await db.select("pob_order_items", filters={"order_id": order_id})
    order = rows[0]
    return respond("OK", {
        "order_id": order_id,
        "status": order.get("status"),
        "total_amount": num(order.get("total_amount")),
        "contact_name": order.get("contact_name"),
        "stockist_name": order.get("stockist_name"),
        "employee_id": order.get("employee_id"),
        "items": [
            {"product_name": item.get("product_name"), "quantity": item.get("quantity"), "rate": num(item.get("rate")), "line_total": num(item.get("line_total"))}
            for item in items
        ],
    })


@router.get("/approvals/dpr")
async def dpr_queue(user: dict = Depends(admin_user), db: DB = Depends(get_db), status: str = "pending"):
    del user
    rows = await db.select("dpr_reports", order="report_date.desc")
    names = await _name_map(db)
    items = [_dpr_list_item(row, names) for row in rows if not status or row.get("status") == status]
    return respond("OK", {"items": items})


@router.get("/approvals/dpr/{dpr_id}")
async def dpr_detail(dpr_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    return respond("OK", await _dpr_detail(db, dpr_id))


@router.post("/approvals/dpr/{dpr_id}/{action}")
async def dpr_action(dpr_id: str, action: str, body: dict = Body(default={}), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    status = {"approve": "approved", "reject": "rejected", "pending": "pending"}.get(action)
    if not status:
        raise APIError(422, "Action must be approve, reject, or pending", "VALIDATION_ERROR")
    data = await _set_dpr_status(db, dpr_id, status, str(body.get("review_note") or ""))
    return respond("DPR updated", data)


@router.get("/approvals/tours")
async def tour_queue(user: dict = Depends(admin_user), db: DB = Depends(get_db), status: str = "pending"):
    del user
    rows = await db.select("approval_requests", filters={"request_type": "tour"}, order="request_date.desc")
    names = await _name_map(db)
    items = [_tour_queue_item(row, names) for row in rows if not status or row.get("status") == status]
    return respond("OK", {"items": items})


@router.get("/approvals/tours/{approval_id}")
async def tour_detail(approval_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("approval_requests", filters={"approval_id": approval_id}, limit=1)
    if not rows:
        raise APIError(404, "Tour approval not found", "NOT_FOUND")
    approval = rows[0]
    names = await _name_map(db)
    person = names.get(approval.get("employee_id")) or {}
    month = str(approval.get("request_date") or "")[:7]
    plans = await db.select("tour_plans", filters={"employee_id": approval.get("employee_id")}, order="plan_date.asc")
    if len(month) == 7 and approval.get("tour_type") == "Monthly Tour Plan":
        start, end = month_bounds(month)
        plans = [row for row in plans if in_month(row.get("plan_date"), start, end)]
    days = []
    for row in plans:
        view = _tour_view(row)
        days.append({
            "plan_id": view["plan_id"],
            "plan_date": view["plan_date"],
            "status": view["status"],
            "areas": [area.get("name") for area in view["selected_areas"] if isinstance(area, dict)],
            "customers": [customer.get("name") for customer in view["customers"] if isinstance(customer, dict)],
        })
    return respond("OK", {
        "approval_id": approval_id,
        "status": approval.get("status"),
        "employee": {"employee_id": approval.get("employee_id"), "name": person.get("name") or "", "territory": person.get("territory") or ""},
        "month": month,
        "days": days,
    })


@router.post("/approvals/tours/{approval_id}/{action}")
async def tour_action(approval_id: str, action: str, body: dict = Body(default={}), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    status = {"approve": "approved", "reject": "rejected"}.get(action)
    if not status:
        raise APIError(422, "Action must be approve or reject", "VALIDATION_ERROR")
    return respond("Tour plan updated", await _review_tour(db, approval_id, status, str(body.get("review_note") or "")))


@router.get("/approvals/leaves")
async def leave_queue(user: dict = Depends(admin_user), db: DB = Depends(get_db), status: str = "pending"):
    del user
    rows = await db.select("approval_requests", filters={"request_type": "leave"}, order="applied_at.desc")
    names = await _name_map(db)
    items = []
    for row in rows:
        if status and row.get("status") != status:
            continue
        item = _leave_item(row)
        person = names.get(row.get("employee_id")) or {}
        item["employee_name"] = person.get("name") or ""
        items.append(item)
    return respond("OK", {"items": items})


@router.post("/approvals/leaves/{approval_id}/{action}")
async def leave_action(approval_id: str, action: str, body: dict = Body(default={}), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    status = {"approve": "approved", "reject": "rejected"}.get(action)
    if not status:
        raise APIError(422, "Action must be approve or reject", "VALIDATION_ERROR")
    data = await _review_leave(db, approval_id, status, str(body.get("review_note") or ""))
    return respond("Leave updated", data)


@router.get("/leaves/balances")
async def balances(user: dict = Depends(admin_user), db: DB = Depends(get_db), employee_id: str = ""):
    del user
    if employee_id:
        row = await _balance(db, employee_id)
        rows = [row]
    else:
        rows = await db.select("leave_balances")
    items = [
        {
            "employee_id": row.get("employee_id"),
            "casual_leave": num(row.get("casual_leave")),
            "casual_leave_used": num(row.get("casual_leave_used")),
            "earned_leave": num(row.get("earned_leave")),
            "earned_leave_used": num(row.get("earned_leave_used")),
            "medical_leave": num(row.get("medical_leave")),
            "medical_leave_used": num(row.get("medical_leave_used")),
            "compensatory_leave": num(row.get("compensatory_leave")),
            "compensatory_leave_used": num(row.get("compensatory_leave_used")),
        }
        for row in rows
    ]
    return respond("OK", {"items": items})


@router.get("/attendance")
async def attendance_report(user: dict = Depends(admin_user), db: DB = Depends(get_db), date_from: str = Query(default="", alias="from"), date_to: str = Query(default="", alias="to"), employee_id: str = ""):
    del user
    filters = {"employee_id": employee_id} if employee_id else None
    rows = await db.select("attendance_records", filters=filters, order="attendance_date.desc")
    names = await _name_map(db)
    items = []
    for row in rows:
        day = as_date(row.get("attendance_date"))
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        person = names.get(row.get("employee_id")) or {}
        items.append({
            "employee_id": row.get("employee_id"),
            "employee_name": person.get("name") or "",
            "attendance_date": day,
            "check_in_time": row.get("check_in"),
            "check_out_time": None if row.get("check_out") in ("—", "", None) else row.get("check_out"),
            "status": row.get("attendance_status") or row.get("status"),
            "review_status": row.get("review_status") or "",
            "address_in": row.get("address_in") or "",
        })
    return respond("OK", {"items": items})


@router.get("/approvals/attendance")
async def attendance_queue(user: dict = Depends(admin_user), db: DB = Depends(get_db), review_status: str = "pending"):
    del user
    rows = await db.select("attendance_records", filters={"review_status": review_status}, order="attendance_date.desc")
    names = await _name_map(db)
    items = []
    for row in rows:
        person = names.get(row.get("employee_id")) or {}
        items.append({
            "record_id": row.get("id") or f"{row.get('employee_id')}:{as_date(row.get('attendance_date'))}",
            "employee_id": row.get("employee_id"),
            "employee_name": person.get("name") or "",
            "attendance_date": as_date(row.get("attendance_date")),
            "check_in_time": row.get("check_in"),
            "check_out_time": row.get("check_out"),
            "status": row.get("status"),
            "review_status": row.get("review_status"),
            "review_note": row.get("review_note") or "",
            "manual_reason": row.get("manual_reason") or "",
        })
    return respond("OK", {"items": items})


@router.post("/approvals/attendance/{record_id}/{action}")
async def review_attendance(record_id: str, action: str, body: dict = Body(default={}), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    status = {"approve": "approved", "reject": "rejected"}.get(action)
    if not status:
        raise APIError(422, "Action must be approve or reject", "VALIDATION_ERROR")
    employee_id, _, day = record_id.partition(":")
    if not day:
        raise APIError(422, "record_id must be employee_id:YYYY-MM-DD", "VALIDATION_ERROR")
    note = str(body.get("review_note") or "")
    updates = {"review_status": status, "review_note": note}
    if status == "approved":
        updates["status"] = "present"
        updates["attendance_status"] = "present"
    await db.update("attendance_records", updates, {"employee_id": employee_id, "attendance_date": day})
    return respond("Attendance reviewed", {"record_id": record_id, "review_status": status, "status": updates.get("status", ""), "review_note": note})


@router.get("/approvals/expenses/history")
async def expense_history(user: dict = Depends(admin_user), db: DB = Depends(get_db), employee_id: str = ""):
    del user
    filters = {"employee_id": employee_id} if employee_id else None
    rows = await db.select("expense_claims", filters=filters, order="claim_date.desc")
    names = await _name_map(db)
    return respond("OK", {"items": [_claim(row, (names.get(row.get("employee_id")) or {}).get("name") or "") for row in rows]})


@router.get("/approvals/expenses")
async def expense_queue(user: dict = Depends(admin_user), db: DB = Depends(get_db), status: str = "pending"):
    del user
    rows = await db.select("expense_claims", filters={"status": status} if status else None, order="claim_date.desc")
    names = await _name_map(db)
    return respond("OK", {"items": [_claim(row, (names.get(row.get("employee_id")) or {}).get("name") or "") for row in rows]})


@router.get("/approvals/expenses/{claim_id}")
async def expense_detail(claim_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("expense_claims", filters={"claim_id": claim_id}, limit=1)
    if not rows:
        raise APIError(404, "Claim not found", "NOT_FOUND")
    names = await _name_map(db)
    person = names.get(rows[0].get("employee_id")) or {}
    return respond("OK", _claim(rows[0], person.get("name") or ""))


@router.post("/approvals/expenses/{claim_id}/{action}")
async def review_expense(claim_id: str, action: str, body: dict = Body(default={}), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    status = {"approve": "approved", "reject": "rejected"}.get(action)
    if not status:
        raise APIError(422, "Action must be approve or reject", "VALIDATION_ERROR")
    note = str(body.get("review_note") or "")
    saved = await db.update("expense_claims", {"status": status, "review_note": note, "updated_at": now_iso()}, {"claim_id": claim_id})
    if not saved:
        raise APIError(404, "Claim not found", "NOT_FOUND")
    return respond("Expense reviewed", {"claim_id": claim_id, "status": status, "review_note": note})


@router.get("/visits")
async def admin_visits(
    user: dict = Depends(admin_user),
    db: DB = Depends(get_db),
    date_from: str = Query(default="", alias="from"),
    date_to: str = Query(default="", alias="to"),
    employee_id: str = "",
    entity_type: str = "",
    page: int = 1,
    limit: int = 20,
):
    del user
    reports = await db.select("dpr_reports")
    by_id = {row.get("dpr_id"): row for row in reports}
    names = await _name_map(db)
    details = await db.select("dpr_visit_details")
    items = []
    for row in details:
        report = by_id.get(row.get("dpr_id")) or {}
        if employee_id and report.get("employee_id") != employee_id:
            continue
        if entity_type and (row.get("entity_type") or "") != entity_type:
            continue
        day = as_date(report.get("report_date"))
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        person = names.get(report.get("employee_id")) or {}
        items.append({
            "visit_id": row.get("visit_id"),
            "dpr_id": row.get("dpr_id"),
            "report_date": day,
            "employee_id": report.get("employee_id"),
            "employee_name": person.get("name") or "",
            "entity_id": row.get("entity_id") or "",
            "entity_name": row.get("entity_name") or "",
            "entity_type": row.get("entity_type") or "",
            "territory": person.get("territory") or "",
            "status": row.get("status") or report.get("status"),
        })
    page_items, meta = paginate(items, page, limit)
    return respond("OK", {"items": page_items, "pagination": meta})


@router.get("/performance")
async def performance(
    user: dict = Depends(admin_user),
    db: DB = Depends(get_db),
    date_from: str = Query(default="", alias="from"),
    date_to: str = Query(default="", alias="to"),
    territory: str = "",
    mr: str = "",
):
    del user
    users = await db.select("users", filters={"status": "active"})
    if territory:
        users = [row for row in users if row.get("territory") == territory]
    if mr:
        users = [row for row in users if row.get("employee_id") == mr or mr.lower() in (row.get("name") or "").lower()]
    reports = await db.select("dpr_reports")
    details = await db.select("dpr_visit_details")
    orders = await db.select("pob_orders")
    attendance = await db.select("attendance_records")
    by_report = {row.get("dpr_id"): row for row in reports}
    rows_out = []
    summary = {key: 0 for key in ("total_visits", "doctors_covered", "hospitals_covered", "chemists_covered", "distributors_covered", "product_presentations", "pob_value", "orders_booked", "samples_distributed", "working_days", "leave_days")}
    for person in users:
        emp = person.get("employee_id")
        emp_reports = [row for row in reports if row.get("employee_id") == emp and (not date_from or as_date(row.get("report_date")) >= date_from) and (not date_to or as_date(row.get("report_date")) <= date_to)]
        report_ids = {row.get("dpr_id") for row in emp_reports}
        visits = [row for row in details if row.get("dpr_id") in report_ids]
        def covered(kind: str) -> int:
            return len({row.get("entity_name") for row in visits if (row.get("entity_type") or "") == kind})
        emp_orders = [row for row in orders if row.get("employee_id") == emp and row.get("status") in ("submitted", "approved") and (not date_from or as_date(row.get("created_at")) >= date_from) and (not date_to or as_date(row.get("created_at")) <= date_to)]
        emp_att = [row for row in attendance if row.get("employee_id") == emp and (not date_from or as_date(row.get("attendance_date")) >= date_from) and (not date_to or as_date(row.get("attendance_date")) <= date_to)]
        present = sum(1 for row in emp_att if (row.get("attendance_status") or row.get("status")) in ("present", "on_duty", "extended_hours", "half_day"))
        leave_days = sum(1 for row in emp_att if (row.get("status") or "") == "leave")
        working = present
        total_visits = len(visits)
        doctors = covered("doctor")
        hospitals = covered("hospital")
        chemists = covered("chemist")
        distributors = covered("distributor") + covered("stockist")
        pob_value = int(sum(num(row.get("total_amount")) for row in emp_orders))
        last = max((as_date(row.get("report_date")) for row in emp_reports), default="")
        score = min(100, total_visits + (10 if pob_value else 0))
        summary["total_visits"] += total_visits
        summary["doctors_covered"] += doctors
        summary["hospitals_covered"] += hospitals
        summary["chemists_covered"] += chemists
        summary["distributors_covered"] += distributors
        summary["pob_value"] += pob_value
        summary["orders_booked"] += len(emp_orders)
        summary["working_days"] += working
        summary["leave_days"] += leave_days
        rows_out.append({
            "employee_id": emp,
            "name": person.get("name"),
            "territory": person.get("territory") or "",
            "total_visits": total_visits,
            "doctors_covered": doctors,
            "hospitals_covered": hospitals,
            "chemists_covered": chemists,
            "distributors_covered": distributors,
            "product_presentations": total_visits,
            "samples_distributed": 0,
            "pob_value": pob_value,
            "orders_booked": len(emp_orders),
            "attendance_pct": round(present * 100 / len(emp_att)) if emp_att else 0,
            "working_days": working,
            "leave_days": leave_days,
            "last_activity": last,
            "performance_score": score,
        })
    days = summary["working_days"] or 1
    return respond("OK", {
        "summary": {
            **summary,
            "product_presentations": summary["total_visits"],
            "avg_visits_per_day": round(summary["total_visits"] / days, 1),
        },
        "rows": rows_out,
    })


@router.get("/cabinet/folders")
async def cabinet_folders(user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    folders, files = await _folders(db)
    return respond("OK", {"items": _folder_tree(folders, files)})


@router.post("/cabinet/folders")
async def create_folder(body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    folder_id = new_id("f")
    row = {"folder_id": folder_id, "label": body.get("name") or body.get("label") or "Folder", "parent_id": body.get("parent_id")}
    if not row["parent_id"]:
        row.pop("parent_id")
    await db.insert("cabinet_folders", row)
    return respond("Folder created", {"id": folder_id, "name": row["label"], "parent_id": body.get("parent_id"), "file_count": 0}, 201)


@router.patch("/cabinet/folders/{folder_id}")
async def update_folder(folder_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    updates = {}
    if "name" in body or "label" in body:
        updates["label"] = body.get("name") or body.get("label")
    if "parent_id" in body:
        updates["parent_id"] = body.get("parent_id")
    saved = await db.update("cabinet_folders", updates, {"folder_id": folder_id})
    if not saved:
        raise APIError(404, "Folder not found", "NOT_FOUND")
    return respond("Folder updated", _folder_card(saved[0]))


@router.delete("/cabinet/folders/{folder_id}")
async def delete_folder(folder_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    await db.delete("cabinet_folders", {"folder_id": folder_id})
    return respond("Folder deleted", None)


@router.get("/cabinet/folders/{folder_id}/files")
async def folder_files(folder_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db), include_inactive: bool = False):
    del user
    folders = await db.select("cabinet_folders", filters={"folder_id": folder_id}, limit=1)
    if not folders:
        raise APIError(404, "Folder not found", "NOT_FOUND")
    files = await db.select("cabinet_files", filters={"folder_id": folder_id}, order="name.asc")
    if not include_inactive:
        files = [row for row in files if row.get("is_active", True)]
    return respond("OK", {"folder": _folder_card(folders[0], len(files)), "items": [_file_card(row) for row in files]})


@router.post("/cabinet/folders/{folder_id}/files")
async def upload_file(folder_id: str, file: UploadFile = File(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    raw = await file.read()
    file_id = new_id("cf")
    name = file.filename or "file"
    safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in name)
    path = f"{folder_id}/{file_id}-{safe}"
    url = await db.upload("cabinet-files", path, raw, file.content_type or "application/octet-stream")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else "file"
    size = len(raw)
    label = f"{size / (1024 * 1024):.1f} MB" if size >= 1024 * 1024 else f"{max(1, size // 1024)} KB"
    row = {
        "file_id": file_id,
        "folder_id": folder_id,
        "name": name,
        "file_type": ext,
        "size_bytes": size,
        "size_label": label,
        "storage_path": path,
        "file_url": url,
        "uploaded_by": user.get("email") or "Admin",
        "is_active": True,
    }
    await db.insert("cabinet_files", row)
    return respond("File uploaded", {"id": file_id, "folder_id": folder_id, "name": name, "size_label": label, "type": ext, "is_active": True, "preview_url": url}, 201)


@router.patch("/cabinet/files/{file_id}")
async def update_file(file_id: str, body: dict = Body(...), user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    saved = await db.update("cabinet_files", {"is_active": bool(body.get("is_active", True))}, {"file_id": file_id})
    if not saved:
        raise APIError(404, "File not found", "NOT_FOUND")
    return respond("File updated", {"id": file_id, "is_active": saved[0].get("is_active")})


@router.delete("/cabinet/files/{file_id}")
async def delete_file(file_id: str, user: dict = Depends(admin_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("cabinet_files", filters={"file_id": file_id}, limit=1)
    if rows and rows[0].get("storage_path"):
        await db.remove_object("cabinet-files", rows[0]["storage_path"])
    await db.delete("cabinet_files", {"file_id": file_id})
    return respond("File deleted", None)
