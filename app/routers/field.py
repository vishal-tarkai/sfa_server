"""Field-app routes. Paths match API.md Part A."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Query, UploadFile

from app.auth import drop_token, field_user, issue_token
from app.config import (
    EXTENDED_MINUTES,
    FIELD_DEFAULT_PASSWORD,
    MIN_CHECKOUT_MINUTES,
    OFFICE_LAT,
    OFFICE_LNG,
    OFFICE_RADIUS_M,
    PRESENT_MINUTES,
)
from app.db import DB
from app.deps import get_db
from app.errors import APIError, respond
from app.util import (
    area_card,
    as_date,
    clock_label,
    entity_card,
    haversine_m,
    in_month,
    login_user,
    month_bounds,
    new_id,
    normalize_mobile,
    now_iso,
    num,
    paginate,
    period_label,
    product_card,
    profile_user,
    public_user,
    today_str,
    mobile_ok,
)

router = APIRouter(tags=["field"])


async def _user_row(db: DB, employee_id: str) -> dict:
    rows = await db.select("users", filters={"employee_id": employee_id}, limit=1)
    if not rows:
        raise APIError(404, "User not found", "NOT_FOUND")
    return rows[0]


async def _offices(db: DB) -> list[dict]:
    try:
        rows = await db.select("head_offices", filters={"status": "active"})
    except APIError:
        rows = []
    offices = []
    for row in rows:
        offices.append({
            "id": row.get("ho_id") or "",
            "name": row.get("name") or "Head Office",
            "latitude": num(row.get("latitude")),
            "longitude": num(row.get("longitude")),
            "radius_m": num(row.get("radius_meters")) or OFFICE_RADIUS_M,
        })
    if not offices:
        offices.append({
            "id": "env",
            "name": "Head Office",
            "latitude": OFFICE_LAT,
            "longitude": OFFICE_LNG,
            "radius_m": OFFICE_RADIUS_M,
        })
    return offices


def _inside_office(lat: float, lng: float, offices: list[dict]) -> bool:
    for office in offices:
        if haversine_m(lat, lng, office["latitude"], office["longitude"]) <= office["radius_m"]:
            return True
    return False


async def _attendance_today(db: DB, employee_id: str) -> dict | None:
    rows = await db.select(
        "attendance_records",
        filters={"employee_id": employee_id, "attendance_date": today_str()},
        limit=1,
    )
    return rows[0] if rows else None


def _attendance_view(row: dict | None) -> dict:
    if not row:
        return {
            "is_checked_in": False,
            "check_in_time": None,
            "check_out_time": None,
            "working_hours": None,
            "attendance_status": "absent",
            "location_status_in": "online",
            "location_status_out": None,
            "address_in": None,
            "address_out": None,
            "gps_accuracy": 0,
            "latitude_in": None,
            "longitude_in": None,
            "latitude_out": None,
            "longitude_out": None,
            "has_approved_leave": False,
            "extended_hours": False,
            "manual_reasons": [],
        }
    checkout = row.get("check_out")
    checked_out = bool(checkout) and checkout not in ("—", "-", "")
    checkin = row.get("check_in")
    return {
        "is_checked_in": bool(checkin) and checkin not in ("—", "") and not checked_out,
        "check_in_time": checkin if checkin not in ("—", "") else None,
        "check_out_time": checkout if checked_out else None,
        "working_hours": row.get("duration") if row.get("duration") not in ("—", "", None) else None,
        "attendance_status": row.get("attendance_status") or row.get("status") or "absent",
        "location_status_in": row.get("location_status_in") or "online",
        "location_status_out": row.get("location_status_out") or None,
        "address_in": row.get("address_in"),
        "address_out": row.get("address_out"),
        "gps_accuracy": row.get("gps_accuracy") or 0,
        "latitude_in": row.get("latitude_in"),
        "longitude_in": row.get("longitude_in"),
        "latitude_out": row.get("latitude_out"),
        "longitude_out": row.get("longitude_out"),
        "has_approved_leave": False,
        "extended_hours": bool(row.get("extended_hours")),
        "manual_reasons": [row.get("manual_reason")] if row.get("manual_reason") else [],
    }


async def _org_nodes(db: DB) -> list[dict]:
    return await db.select("org_nodes", order="label.asc")


async def _assigned_areas(db: DB, employee_id: str) -> list[dict]:
    user = await _user_row(db, employee_id)
    nodes = await _org_nodes(db)
    by_id = {node.get("node_id"): node for node in nodes}
    children: dict[str | None, list] = {}
    for node in nodes:
        children.setdefault(node.get("parent_id"), []).append(node)
    root_id = user.get("org_node_id")
    picked: list[dict] = []
    if root_id and root_id in by_id:
        stack = [root_id]
        seen = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            node = by_id.get(current)
            if node:
                picked.append(node)
            for child in children.get(current, []):
                stack.append(child.get("node_id"))
    if not picked:
        territory = (user.get("territory") or "").strip().lower()
        picked = [
            node for node in nodes
            if (node.get("label") or "").strip().lower() == territory
            or (node.get("node_type") or "") == "area"
        ]
    return [area_card(node) for node in picked]


def _claim(row: dict, name: str = "") -> dict:
    return {
        "claim_id": row.get("claim_id"),
        "employee_id": row.get("employee_id"),
        "employee_name": name,
        "claim_date": as_date(row.get("claim_date")),
        "status": row.get("status") or "pending",
        "miscellaneous": num(row.get("miscellaneous")),
        "phone_allowance": num(row.get("phone_allowance")),
        "internet_reimbursement": num(row.get("internet_reimbursement")),
        "total_amount": num(row.get("total_amount")),
        "comment": row.get("comment") or "",
        "review_note": row.get("review_note") or "",
        "submitted_at": row.get("submitted_at"),
    }


def _order_summary(row: dict, name: str = "", territory: str = "") -> dict:
    return {
        "order_id": row.get("order_id"),
        "employee_id": row.get("employee_id"),
        "employee_name": name or row.get("employee_name") or "",
        "territory": territory or row.get("territory") or "",
        "pv_type": row.get("pv_type") or "",
        "contact_name": row.get("contact_name") or "",
        "phone": row.get("phone") or "",
        "stockist_name": row.get("stockist_name") or "",
        "total_amount": num(row.get("total_amount")),
        "item_count": row.get("item_count") or 0,
        "status": row.get("status") or "submitted",
        "submitted_at": row.get("created_at") or row.get("submitted_at"),
    }


def _folder_card(row: dict, file_count: int = 0) -> dict:
    return {
        "id": row.get("folder_id"),
        "name": row.get("label") or row.get("name") or "",
        "file_count": file_count,
        "size_label": row.get("size_label") or "",
        "parent_id": row.get("parent_id"),
        "children": [],
    }


def _file_card(row: dict, folder_name: str = "") -> dict:
    return {
        "id": row.get("file_id"),
        "folder_id": row.get("folder_id"),
        "name": row.get("name") or "",
        "size_label": row.get("size_label") or "",
        "modified_label": str(row.get("uploaded_at") or "")[:10],
        "type": row.get("file_type") or "",
        "folder_name": folder_name or None,
        "preview_url": row.get("file_url"),
        "is_active": row.get("is_active", True),
    }


async def _name_map(db: DB) -> dict[str, dict]:
    rows = await db.select("users", select="employee_id,name,territory,designation,role")
    return {row.get("employee_id"): row for row in rows}


# --- auth & profile -------------------------------------------------------

@router.post("/auth/login")
async def login(
    body: dict = Body(
        ...,
        openapi_examples={
            "field": {
                "summary": "Field MR login",
                "value": {
                    "mobile": "7988521618",
                    "password": "1234",
                    "country_code": "+91",
                },
            }
        },
    ),
    db: DB = Depends(get_db),
):
    mobile = str(body.get("mobile") or "").strip()
    password = str(body.get("password") or "")
    if not mobile_ok(mobile):
        raise APIError(422, "Please enter a valid mobile number", "VALIDATION_ERROR", {"mobile": ["Please enter a valid mobile number"]})
    if not password:
        raise APIError(422, "Please enter your password", "VALIDATION_ERROR", {"password": ["Please enter your password"]})
    rows = await db.select("users", filters={"status": "active"})
    wanted = normalize_mobile(mobile)
    match = None
    for row in rows:
        stored = str(row.get("password") or "").strip() or FIELD_DEFAULT_PASSWORD
        if normalize_mobile(str(row.get("mobile") or "")) == wanted and stored == password:
            match = row
            break
    if match is None:
        raise APIError(401, "Invalid mobile number or password.", "INVALID_CREDENTIALS", {"password": ["Invalid mobile number or password."]})
    if (match.get("status") or "") != "active":
        raise APIError(403, "Your account is not active. Contact your manager.", "ACCOUNT_DISABLED")
    if not str(match.get("password") or "").strip():
        await db.update("users", {"password": password}, {"employee_id": match["employee_id"]})
    stamp = datetime.now().strftime("%d %b, %H:%M")
    await db.update("users", {"last_login": stamp}, {"employee_id": match["employee_id"]})
    match["last_login"] = stamp
    token, expires = issue_token("field", employee_id=match["employee_id"])
    return respond("Login successful", {"access_token": token, "expires_in": expires, "user": login_user(match)})


@router.post("/auth/logout")
async def logout(user: dict = Depends(field_user)):
    drop_token(user.get("token"))
    return respond("Logged out successfully", None)


@router.post("/auth/fcm-token")
async def register_fcm(body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    token = str(body.get("token") or "").strip()
    if not token:
        raise APIError(422, "Token is required", "VALIDATION_ERROR", {"token": ["Token is required"]})
    platform = body.get("platform") or "android"
    await db.upsert("user_fcm_tokens", {
        "fcm_token": token,
        "employee_id": user["employee_id"],
        "platform": platform,
        "updated_at": now_iso(),
    }, "fcm_token")
    return respond("Token registered", {"employee_id": user["employee_id"], "token": token, "platform": platform})


@router.delete("/auth/fcm-token")
async def remove_fcm(user: dict = Depends(field_user), db: DB = Depends(get_db), token: str = Query(default="")):
    del user
    value = token
    if value:
        await db.delete("user_fcm_tokens", {"fcm_token": value})
    return respond("Token removed", None)


@router.get("/users/me")
async def me(user: dict = Depends(field_user), db: DB = Depends(get_db)):
    row = await _user_row(db, user["employee_id"])
    return respond("OK", profile_user(row))


@router.patch("/users/me")
async def patch_me(body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    updates = {}
    if "visit_reminders_enabled" in body:
        updates["visit_reminders_enabled"] = bool(body["visit_reminders_enabled"])
    if updates:
        try:
            await db.update("users", updates, {"employee_id": user["employee_id"]})
        except APIError:
            pass
    row = await _user_row(db, user["employee_id"])
    row.update(updates)
    return respond("Profile updated", profile_user(row))


@router.post("/users/me/avatar")
async def upload_avatar(file: UploadFile = File(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    raw = await file.read()
    ext = (file.filename or "avatar.jpg").rsplit(".", 1)[-1][:5]
    path = f"{user['employee_id']}/avatar.{ext}"
    url = await db.upload("user-avatars", path, raw, file.content_type or "image/jpeg")
    await db.update("users", {"profile_image_url": url, "updated_at": now_iso()}, {"employee_id": user["employee_id"]})
    return respond("Avatar updated", {"profile_image_url": url})


@router.put("/users/me/biometric")
async def save_biometric(body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    embedding = body.get("face_embedding")
    if embedding is None:
        await db.update("users", {"biometric": False, "face_embedding": None}, {"employee_id": user["employee_id"]})
        return respond("Biometric cleared", {"biometric_enrolled": False})
    if not isinstance(embedding, list):
        raise APIError(422, "face_embedding must be a list of numbers", "VALIDATION_ERROR")
    stored = ",".join(str(float(item)) for item in embedding)
    await db.update("users", {"biometric": True, "face_embedding": stored}, {"employee_id": user["employee_id"]})
    return respond("Biometric saved", {"biometric_enrolled": True})


# --- home -----------------------------------------------------------------

@router.get("/dashboard")
async def dashboard(user: dict = Depends(field_user), db: DB = Depends(get_db)):
    employee_id = user["employee_id"]
    attendance = _attendance_view(await _attendance_today(db, employee_id))
    period = period_label()
    targets = await db.select("sales_targets", filters={"employee_id": employee_id, "period": period}, limit=1)
    target_amount = int(num(targets[0].get("target_amount"))) if targets else 0
    start, end = month_bounds(date.today().strftime("%Y-%m"))
    orders = await db.select("pob_orders", filters={"employee_id": employee_id})
    achieved = sum(num(row.get("total_amount")) for row in orders if row.get("status") in ("submitted", "approved") and in_month(row.get("created_at"), start, end))
    percent = round(achieved * 100 / target_amount) if target_amount else 0
    plans = await db.select("tour_plans", filters={"employee_id": employee_id, "plan_date": today_str()}, limit=1)
    upcoming = []
    if plans:
        for customer in plans[0].get("customers") or []:
            if isinstance(customer, dict):
                upcoming.append({
                    "entity_id": customer.get("id") or "",
                    "name": customer.get("name") or "",
                    "type": customer.get("type") or "",
                    "time": customer.get("time") or "",
                    "area": customer.get("area") or "",
                })
    notes = await db.select("user_notifications", filters={"employee_id": employee_id, "is_read": "eq.false"})
    celebrations = await _celebration_items(db)
    return respond("OK", {
        "employee_id": employee_id,
        "date": today_str(),
        "attendance": {
            "is_checked_in": attendance["is_checked_in"],
            "check_in_time": attendance["check_in_time"],
            "check_out_time": attendance["check_out_time"],
            "attendance_status": attendance["attendance_status"],
        },
        "target": {
            "period": period,
            "monthly_target_inr": target_amount,
            "achieved_inr": int(achieved),
            "achievement_percent": percent,
        },
        "upcoming_visits": upcoming[:8],
        "celebrations_today": len(celebrations),
        "unread_notifications": len(notes),
    })


@router.get("/dashboard/schedule")
async def schedule(user: dict = Depends(field_user), db: DB = Depends(get_db), day: str = Query(default="", alias="date")):
    day = day or today_str()
    plans = await db.select("tour_plans", filters={"employee_id": user["employee_id"], "plan_date": day}, limit=1)
    plan = plans[0] if plans else {}
    visited = set()
    reports = await db.select("dpr_reports", filters={"employee_id": user["employee_id"], "report_date": day})
    if reports:
        visits = await db.select("dpr_visit_details", filters={"dpr_id": reports[0].get("dpr_id")})
        visited = {row.get("entity_name") for row in visits}
    items = []
    for customer in plan.get("customers") or []:
        if not isinstance(customer, dict):
            continue
        name = customer.get("name") or ""
        items.append({
            "entity_id": customer.get("id") or "",
            "name": name,
            "type": customer.get("type") or "",
            "planned": True,
            "visited": name in visited,
        })
    return respond("OK", {
        "date": day,
        "plan_status": plan.get("status"),
        "areas": plan.get("selected_areas") or [],
        "visits": items,
    })


@router.get("/dashboard/proximity")
async def proximity(user: dict = Depends(field_user), db: DB = Depends(get_db), lat: float = Query(...), lng: float = Query(...)):
    del user
    rows = await db.select("healthcare_entities", filters={"status": "active"})
    items = []
    for row in rows:
        elat, elng = row.get("latitude"), row.get("longitude")
        if elat is None or elng is None:
            continue
        items.append({
            "id": row.get("entity_id"),
            "name": row.get("name"),
            "type": row.get("type"),
            "latitude": num(elat),
            "longitude": num(elng),
            "distance_m": round(haversine_m(lat, lng, num(elat), num(elng))),
        })
    items.sort(key=lambda item: item["distance_m"])
    return respond("OK", {"items": items[:50]})


async def _celebration_items(db: DB) -> list[dict]:
    today = date.today()
    key = today.strftime("%m-%d")
    rows = await db.select("healthcare_entities", filters={"status": "active"})
    items = []
    for row in rows:
        for field, occasion in (("dob", "birthday"), ("anniversary", "anniversary")):
            raw = str(row.get(field) or "")
            if len(raw) >= 10 and raw[5:10] == key:
                items.append({
                    "entity_id": row.get("entity_id"),
                    "name": row.get("name"),
                    "type": row.get("type"),
                    "occasion": occasion,
                    "hospital": row.get("hospital") or "",
                    "phone": row.get("phone") or "",
                })
    return items


@router.get("/celebrations/today")
async def celebrations(user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    return respond("OK", {"items": await _celebration_items(db)})


@router.get("/sales/targets")
async def sales_targets(user: dict = Depends(field_user), db: DB = Depends(get_db), period: str = Query(default="")):
    employee_id = user["employee_id"]
    period = period or period_label()
    rows = await db.select("sales_targets", filters={"employee_id": employee_id})
    current = next((row for row in rows if row.get("period") == period), None)
    if current is None and rows:
        published = [row for row in rows if row.get("status") == "published"]
        current = published[-1] if published else rows[-1]
        period = current.get("period") or period
    target_amount = int(num(current.get("target_amount"))) if current else 0
    orders = await db.select("pob_orders", filters={"employee_id": employee_id})
    counted = [row for row in orders if row.get("status") in ("submitted", "approved")]

    def achieved_for(label: str) -> int:
        try:
            name, year = label.split()
            month = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"].index(name) + 1
            start, end = month_bounds(f"{year}-{month:02d}")
        except ValueError:
            return 0
        return int(sum(num(row.get("total_amount")) for row in counted if in_month(row.get("created_at"), start, end)))

    achieved = achieved_for(period)
    products = []
    for item in (current or {}).get("products") or []:
        if isinstance(item, dict):
            products.append({
                "name": item.get("name") or item.get("product_name") or "",
                "target_inr": int(num(item.get("target_amount") or item.get("target_inr"))),
                "achieved_inr": int(num(item.get("achieved_amount") or item.get("achieved_inr"))),
                "achievement_percent": int(num(item.get("achievement_percent"))),
            })
    chart = []
    anchor = date.today()
    for offset in range(4, -1, -1):
        moment = date(anchor.year, anchor.month, 1)
        month_index = moment.month - offset
        year = moment.year
        while month_index <= 0:
            month_index += 12
            year -= 1
        label = period_label(date(year, month_index, 1))
        row = next((item for item in rows if item.get("period") == label), None)
        chart.append({
            "month": label,
            "target_inr": int(num((row or {}).get("target_amount"))),
            "achieved_inr": achieved_for(label),
            "is_current": label == period,
        })
    order_count = 0
    try:
        name, year = period.split()
        month = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"].index(name) + 1
        start, end = month_bounds(f"{year}-{month:02d}")
        order_count = sum(1 for row in counted if in_month(row.get("created_at"), start, end))
    except ValueError:
        order_count = len(counted)
    percent = round(achieved * 100 / target_amount, 1) if target_amount else 0
    return respond("OK", {
        "period": period,
        "status": (current or {}).get("status") or "draft",
        "summary": {
            "monthly_target_inr": target_amount,
            "achieved_inr": achieved,
            "achievement_percent": percent,
            "order_count": order_count,
        },
        "monthly_chart": chart,
        "products": products,
    })


# --- attendance -----------------------------------------------------------

@router.get("/attendance/today")
async def attendance_today(user: dict = Depends(field_user), db: DB = Depends(get_db)):
    row = await _attendance_today(db, user["employee_id"])
    view = _attendance_view(row)
    leaves = await db.select("approval_requests", filters={"employee_id": user["employee_id"], "request_type": "leave", "status": "approved"})
    view["has_approved_leave"] = any(today_str() >= as_date(item.get("leave_from")) and today_str() <= as_date(item.get("leave_to")) for item in leaves if item.get("leave_from"))
    return respond("OK", view)


@router.post("/attendance/check-in")
async def check_in(body: dict = Body(default={}), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    existing = await _attendance_today(db, user["employee_id"])
    view = _attendance_view(existing)
    if view["is_checked_in"] or (view["check_in_time"] and not view["check_out_time"]):
        raise APIError(409, "Already checked in today", "ALREADY_CHECKED_IN")
    force = bool(body.get("force_offline"))
    lat = num(body.get("latitude"))
    lng = num(body.get("longitude"))
    offices = await _offices(db)
    if not force:
        if not lat and not lng:
            raise APIError(422, "Location is required to check in", "VALIDATION_ERROR")
        if not _inside_office(lat, lng, offices):
            raise APIError(422, "You are outside the allowed office radius", "OUTSIDE_GEOFENCE")
    label = clock_label()
    row = {
        "employee_id": user["employee_id"],
        "attendance_date": today_str(),
        "check_in": label,
        "check_out": "—",
        "duration": "—",
        "gps_accuracy": int(num(body.get("gps_accuracy"))),
        "status": "on_duty",
        "attendance_status": "on_duty",
        "latitude_in": lat,
        "longitude_in": lng,
        "location_status_in": "offline" if force else "online",
        "address_in": body.get("address") or body.get("manual_reason") or "",
        "check_in_at": now_iso(),
        "device_time_in": label,
        "extended_hours": False,
        "working_hours_minutes": 0,
        "updated_at": now_iso(),
    }
    if force:
        row["review_status"] = "pending"
        row["manual_reason"] = body.get("manual_reason") or "Offline check-in"
    await db.upsert("attendance_records", row, "employee_id,attendance_date")
    return respond("Checked in", {
        "is_checked_in": True,
        "check_in_time": label,
        "check_out_time": None,
        "location_status": "offline" if force else "online",
        "address": row["address_in"],
        "attendance_status": "on_duty",
        "working_hours": None,
        "extended_hours": False,
    })


@router.post("/attendance/check-out")
async def check_out(body: dict = Body(default={}), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    existing = await _attendance_today(db, user["employee_id"])
    view = _attendance_view(existing)
    if not view["check_in_time"] or view["check_out_time"]:
        raise APIError(409, "Check in before check out", "NOT_CHECKED_IN")
    start = existing.get("check_in_at")
    started = datetime.fromisoformat(str(start).replace("Z", "+00:00")) if start else datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    minutes = max(0, int((datetime.now(timezone.utc) - started).total_seconds() // 60))
    if minutes < MIN_CHECKOUT_MINUTES:
        raise APIError(422, "Minimum 3 working hours are required before Check-Out.", "MIN_HOURS_NOT_MET")
    force = bool(body.get("force_offline"))
    lat = num(body.get("latitude")) or num(existing.get("latitude_in"))
    lng = num(body.get("longitude")) or num(existing.get("longitude_in"))
    if not force and lat and lng and not _inside_office(lat, lng, await _offices(db)):
        raise APIError(422, "You are outside the allowed office radius", "OUTSIDE_GEOFENCE")
    extended = minutes > EXTENDED_MINUTES
    status = "offline_attendance" if force or existing.get("location_status_in") == "offline" else ("extended_hours" if extended else "present" if minutes >= PRESENT_MINUTES else "half_day")
    label = clock_label()
    hours, mins = divmod(minutes, 60)
    duration = f"{hours:02d}:{mins:02d}"
    await db.update("attendance_records", {
        "check_out": label,
        "duration": duration,
        "working_hours_minutes": minutes,
        "attendance_status": status,
        "status": status,
        "extended_hours": extended,
        "latitude_out": lat,
        "longitude_out": lng,
        "location_status_out": "offline" if force else "online",
        "address_out": body.get("address") or "",
        "check_out_at": now_iso(),
        "device_time_out": label,
        "updated_at": now_iso(),
    }, {"employee_id": user["employee_id"], "attendance_date": today_str()})
    return respond("Checked out", {
        "is_checked_in": False,
        "check_in_time": view["check_in_time"],
        "check_out_time": label,
        "location_status": "offline" if force else "online",
        "address": body.get("address") or "",
        "attendance_status": status,
        "working_hours": duration,
        "extended_hours": extended,
    })


@router.get("/attendance/report")
async def attendance_report(user: dict = Depends(field_user), db: DB = Depends(get_db), month: str = Query(default="")):
    month = month or date.today().strftime("%Y-%m")
    start, end = month_bounds(month)
    rows = await db.select("attendance_records", filters={
        "employee_id": user["employee_id"],
        "attendance_date": f"gte.{start}",
    }, order="attendance_date.desc")
    logs = []
    stats: dict[str, int] = {}
    for row in rows:
        if not in_month(row.get("attendance_date"), start, end):
            continue
        status = row.get("attendance_status") or row.get("status") or "absent"
        stats[status] = stats.get(status, 0) + 1
        day = as_date(row.get("attendance_date"))
        logs.append({
            "month": period_label(date.fromisoformat(day)) if day else month,
            "day": date.fromisoformat(day).strftime("%a") if day else "",
            "date": day,
            "check_in": row.get("check_in"),
            "check_out": None if row.get("check_out") in ("—", "", None) else row.get("check_out"),
            "is_late": status in ("late", "half_day"),
            "duration": None if row.get("duration") in ("—", "", None) else row.get("duration"),
            "status": status,
            "holiday_name": row.get("holiday_name"),
            "location_status_in": row.get("location_status_in"),
            "location_status_out": row.get("location_status_out"),
            "extended_hours": bool(row.get("extended_hours")),
            "has_approved_leave": status == "leave",
        })
    return respond("OK", {"stats": stats, "logs": logs})


@router.post("/attendance/manual")
async def manual_attendance(body: dict = Body(default={}), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    payload = dict(body or {})
    payload["force_offline"] = True
    if not payload.get("manual_reason"):
        payload["manual_reason"] = "Manual / offline attendance"
    existing = await _attendance_today(db, user["employee_id"])
    view = _attendance_view(existing)
    if view["check_in_time"] and not view["check_out_time"]:
        return await check_out(payload, user, db)
    return await check_in(payload, user, db)


@router.get("/attendance/head-offices")
async def head_offices(user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    return respond("OK", {"items": await _offices(db)})


@router.get("/visits")
async def visits(
    user: dict = Depends(field_user),
    db: DB = Depends(get_db),
    date_from: str = Query(default="", alias="from"),
    date_to: str = Query(default="", alias="to"),
    page: int = 1,
    limit: int = 20,
):
    reports = await db.select("dpr_reports", filters={"employee_id": user["employee_id"]}, order="report_date.desc")
    report_ids = {row.get("dpr_id") for row in reports}
    by_report = {row.get("dpr_id"): row for row in reports}
    details = await db.select("dpr_visit_details", order="sort_order.asc")
    items = []
    for row in details:
        if row.get("dpr_id") not in report_ids:
            continue
        report = by_report.get(row.get("dpr_id")) or {}
        day = as_date(report.get("report_date"))
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        items.append({
            "visit_id": row.get("visit_id"),
            "dpr_id": row.get("dpr_id"),
            "report_date": day,
            "entity_id": row.get("entity_id") or "",
            "entity_name": row.get("entity_name") or "",
            "entity_type": row.get("entity_type") or "",
            "area": row.get("location_label") or "",
            "status": row.get("status") or report.get("status") or "pending",
        })
    page_items, meta = paginate(items, page, limit)
    return respond("OK", {"items": page_items, "pagination": meta})


# --- network --------------------------------------------------------------

@router.get("/network/entities")
async def list_entities(
    user: dict = Depends(field_user),
    db: DB = Depends(get_db),
    territory: str = "",
    type: str = "",
    q: str = "",
    page: int = 1,
    limit: int = 20,
    entity_id: str | None = None,
):
    del user
    rows = await db.select("healthcare_entities", order="name.asc")
    names = {row.get("entity_id"): row.get("name") for row in rows}
    items = []
    for row in rows:
        if territory and (row.get("territory") or "") != territory:
            continue
        if type and (row.get("type") or "") != type:
            continue
        if q and q.lower() not in (row.get("name") or "").lower():
            continue
        card = entity_card(row, names.get(row.get("stockist_id"), "") or "")
        items.append(card)
    page_items, meta = paginate(items, page, limit)
    return respond("OK", {"items": page_items, "pagination": meta})


@router.get("/network/entities/{entity_id}")
async def get_entity(entity_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("healthcare_entities", filters={"entity_id": entity_id}, limit=1)
    if not rows:
        raise APIError(404, "Entity not found", "NOT_FOUND")
    stockist = ""
    if rows[0].get("stockist_id"):
        linked = await db.select("healthcare_entities", filters={"entity_id": rows[0]["stockist_id"]}, limit=1)
        stockist = linked[0].get("name") if linked else ""
    return respond("OK", entity_card(rows[0], stockist))


@router.post("/network/entities")
async def create_entity(body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    entity_id = body.get("entity_id") or body.get("id") or new_id("ENT")
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
        "dob": body.get("dob") or "",
        "anniversary": body.get("anniversary") or "",
        "stockist_id": body.get("stockist_id") or "",
    }
    saved = await db.insert("healthcare_entities", row)
    return respond("Entity created", entity_card(saved[0] if saved else row), 201)


@router.patch("/network/entities/{entity_id}")
async def update_entity(entity_id: str, body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    allowed = ("name", "type", "specialty", "territory", "city", "phone", "status", "hospital", "tier", "dob", "anniversary", "stockist_id")
    updates = {key: body[key] for key in allowed if key in body}
    saved = await db.update("healthcare_entities", updates, {"entity_id": entity_id})
    if not saved:
        raise APIError(404, "Entity not found", "NOT_FOUND")
    return respond("Entity updated", entity_card(saved[0]))


# --- DPR ------------------------------------------------------------------

@router.get("/dpr/areas/search")
async def dpr_areas(user: dict = Depends(field_user), db: DB = Depends(get_db), q: str = ""):
    items = await _assigned_areas(db, user["employee_id"])
    if q:
        items = [item for item in items if q.lower() in item["name"].lower()]
    return respond("OK", {"items": items})


@router.get("/dpr/entry")
async def dpr_entry(user: dict = Depends(field_user), db: DB = Depends(get_db), day: str = Query(default="", alias="date")):
    day = day or today_str()
    stations = await _assigned_areas(db, user["employee_id"])
    existing = await db.select("dpr_reports", filters={"employee_id": user["employee_id"], "report_date": day}, limit=1)
    row = existing[0] if existing else None
    return respond("OK", {
        "report_date": day,
        "existing_dpr_id": None if not row else row.get("dpr_id"),
        "status": None if not row else row.get("status"),
        "stations": stations,
        "return_to_hq": bool((row or {}).get("return_to_hq")),
    })


@router.get("/dpr/seniors")
async def dpr_seniors(user: dict = Depends(field_user), db: DB = Depends(get_db), q: str = ""):
    rows = await db.select("users", filters={"status": "active"}, order="name.asc")
    items = []
    for row in rows:
        if row.get("employee_id") == user["employee_id"]:
            continue
        if q and q.lower() not in (row.get("name") or "").lower():
            continue
        items.append({
            "id": row.get("employee_id"),
            "name": row.get("name"),
            "role": row.get("role") or row.get("designation") or "",
            "profile_image_url": row.get("profile_image_url") or "",
        })
    return respond("OK", {"items": items})


@router.get("/dpr/doctors/search")
async def dpr_doctors(user: dict = Depends(field_user), db: DB = Depends(get_db), q: str = "", area_id: str = ""):
    del user, area_id
    rows = await db.select("healthcare_entities", filters={"status": "active"}, order="name.asc")
    items = []
    for row in rows:
        if q and q.lower() not in (row.get("name") or "").lower() and q.lower() not in (row.get("hospital") or "").lower():
            continue
        items.append({
            "id": row.get("entity_id"),
            "name": row.get("name"),
            "type": row.get("type"),
            "specialty": row.get("specialty") or "",
            "hospital": row.get("hospital") or "",
            "city": row.get("city") or "",
        })
    return respond("OK", {"items": items[:50]})


def _dpr_list_item(row: dict, names: dict[str, dict]) -> dict:
    person = names.get(row.get("employee_id")) or {}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    doctors = payload.get("doctors") or []
    parties = payload.get("parties") or []
    return {
        "dpr_id": row.get("dpr_id"),
        "report_date": as_date(row.get("report_date")),
        "employee_id": row.get("employee_id"),
        "employee_name": person.get("name") or "",
        "territory": person.get("territory") or "",
        "status": row.get("status") or "pending",
        "visit_count": int(num(row.get("doctor_calls"))) + int(num(row.get("chemist_visits"))) or (len(doctors) + len(parties)),
        "submitted_at": row.get("submitted_time"),
    }


@router.post("/dpr")
async def submit_dpr(body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    employee_id = user["employee_id"]
    person = await _user_row(db, employee_id)
    report_date = body.get("report_date") or today_str()
    existing = await db.select("dpr_reports", filters={"employee_id": employee_id, "report_date": report_date}, limit=1)
    dpr_id = (existing[0].get("dpr_id") if existing else None) or body.get("dpr_id") or new_id("DPR")
    doctors = body.get("doctors") or []
    parties = body.get("parties") or []
    summary = f"DPR {report_date} — {len(doctors)} doctor(s) — {len(parties)} party visit(s)"
    payload = {
        "schema_version": "1.0",
        "dpr_id": dpr_id,
        "status": "pending",
        "report_date": report_date,
        "submitted_at": now_iso(),
        "return_to_hq": bool(body.get("return_to_hq")),
        "user": {
            "employee_id": employee_id,
            "name": person.get("name"),
            "role": person.get("role"),
            "territory": person.get("territory"),
            "hq": person.get("territory") or "",
            "designation": person.get("designation") or "",
        },
        "selected_areas": body.get("selected_areas") or [],
        "doctors": doctors,
        "parties": parties,
        "cme_other": body.get("cme_other") or [],
        "joint_working_seniors": body.get("joint_working_seniors") or [],
    }
    row = {
        "employee_id": employee_id,
        "report_date": report_date,
        "submitted_time": clock_label(),
        "status": "pending",
        "sla": "24h",
        "details": summary,
        "doctor_calls": len(doctors),
        "chemist_visits": len(parties),
        "return_to_hq": bool(body.get("return_to_hq")),
        "selected_areas": body.get("selected_areas") or [],
        "payload": payload,
    }
    if existing:
        await db.update("dpr_reports", row, {"dpr_id": dpr_id})
    else:
        row["dpr_id"] = dpr_id
        await db.insert("dpr_reports", row)
    await db.delete("dpr_visit_details", {"dpr_id": dpr_id})
    visit_rows = []
    order = 1
    for entry in doctors + parties:
        entry_id = entry.get("entry_id") or entry.get("entity_id") or str(order)
        visit_rows.append({
            "visit_id": f"{dpr_id}-{entry_id}",
            "dpr_id": dpr_id,
            "visit_time": clock_label(),
            "entity_name": entry.get("name") or entry.get("entity_name") or "",
            "entity_type": entry.get("type") or entry.get("entity_type") or "doctor",
            "latitude": 0,
            "longitude": 0,
            "location_label": entry.get("station_name") or "",
            "notes": (entry.get("visit") or {}).get("remarks") if isinstance(entry.get("visit"), dict) else entry.get("remarks"),
            "sort_order": order,
            "duration": "—",
            "status": "completed",
        })
        order += 1
    if visit_rows:
        await db.insert("dpr_visit_details", visit_rows)
    payload["dpr_id"] = dpr_id
    return respond("DPR submitted", payload, 201)


@router.get("/dpr")
async def my_dpr(user: dict = Depends(field_user), db: DB = Depends(get_db), month: str = ""):
    rows = await db.select("dpr_reports", filters={"employee_id": user["employee_id"]}, order="report_date.desc")
    if month:
        start, end = month_bounds(month)
        rows = [row for row in rows if in_month(row.get("report_date"), start, end)]
    names = await _name_map(db)
    items = [_dpr_list_item(row, names) for row in rows]
    pending = sum(1 for item in items if item["status"] == "pending")
    return respond("OK", {"pending_count": pending, "items": items})


@router.get("/dpr/approvals")
async def dpr_approvals(user: dict = Depends(field_user), db: DB = Depends(get_db), status: str = "pending"):
    me = await _user_row(db, user["employee_id"])
    manager_name = (me.get("name") or "").strip().lower()
    people = await db.select("users")
    team_ids = {
        row.get("employee_id")
        for row in people
        if (row.get("manager") or "").strip().lower() == manager_name and row.get("employee_id") != user["employee_id"]
    }
    rows = await db.select("dpr_reports", order="report_date.desc")
    names = {row.get("employee_id"): row for row in people}
    items = []
    for row in rows:
        if row.get("employee_id") not in team_ids:
            continue
        if status and (row.get("status") or "pending") != status:
            continue
        items.append(_dpr_list_item(row, names))
    pending = sum(1 for item in items if item["status"] == "pending")
    return respond("OK", {"pending_count": pending, "items": items})


async def _dpr_detail(db: DB, dpr_id: str) -> dict:
    rows = await db.select("dpr_reports", filters={"dpr_id": dpr_id}, limit=1)
    if not rows:
        raise APIError(404, "DPR not found", "DPR_NOT_FOUND")
    row = rows[0]
    names = await _name_map(db)
    person = names.get(row.get("employee_id")) or {}
    visits = await db.select("dpr_visit_details", filters={"dpr_id": dpr_id}, order="sort_order.asc")
    return {
        "dpr_id": dpr_id,
        "status": row.get("status") or "pending",
        "report_date": as_date(row.get("report_date")),
        "employee": {
            "employee_id": row.get("employee_id"),
            "name": person.get("name") or "",
            "designation": person.get("designation") or "",
            "territory": person.get("territory") or "",
        },
        "areas": row.get("selected_areas") or [],
        "visits": [
            {
                "visit_id": visit.get("visit_id"),
                "entity_name": visit.get("entity_name"),
                "entity_type": visit.get("entity_type"),
                "status": visit.get("status") or row.get("status"),
            }
            for visit in visits
        ],
        "review_note": row.get("review_note") or "",
        "payload": row.get("payload"),
    }


@router.get("/dpr/approvals/{dpr_id}")
async def dpr_approval_detail(dpr_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    return respond("OK", await _dpr_detail(db, dpr_id))


@router.get("/dpr/{dpr_id}")
async def my_dpr_detail(dpr_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    detail = await _dpr_detail(db, dpr_id)
    if detail.get("employee", {}).get("employee_id") not in (user["employee_id"], None):
        me = await _user_row(db, user["employee_id"])
        owner = (detail.get("employee") or {}).get("employee_id")
        people = await db.select("users", filters={"employee_id": owner}, limit=1)
        manager = ((people[0].get("manager") if people else "") or "").strip().lower()
        if manager != (me.get("name") or "").strip().lower() and owner != user["employee_id"]:
            raise APIError(403, "You cannot view this DPR", "FORBIDDEN")
    return respond("OK", detail)


async def _set_dpr_status(db: DB, dpr_id: str, status: str, note: str = "") -> dict:
    rows = await db.select("dpr_reports", filters={"dpr_id": dpr_id}, limit=1)
    if not rows:
        raise APIError(404, "DPR not found", "DPR_NOT_FOUND")
    if status == "approved" and rows[0].get("status") == "approved":
        raise APIError(409, "DPR is already approved", "DPR_ALREADY_APPROVED")
    updates = {"status": status}
    if note:
        updates["review_note"] = note
    await db.update("dpr_reports", updates, {"dpr_id": dpr_id})
    return {"dpr_id": dpr_id, "status": status, "review_note": note}


@router.post("/dpr/approvals/{dpr_id}/approve")
async def approve_dpr(dpr_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db), body: dict | None = Body(default=None)):
    del user
    data = await _set_dpr_status(db, dpr_id, "approved", str((body or {}).get("review_note") or ""))
    return respond("DPR approved", data)


@router.post("/dpr/approvals/{dpr_id}/reject")
async def reject_dpr(dpr_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db), body: dict | None = Body(default=None)):
    del user
    note = str((body or {}).get("review_note") or "")
    return respond("DPR rejected", await _set_dpr_status(db, dpr_id, "rejected", note))


# --- tour plans -----------------------------------------------------------

def _tour_view(row: dict) -> dict:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return {
        "plan_id": row.get("plan_id"),
        "plan_date": as_date(row.get("plan_date")),
        "status": row.get("status") or "draft",
        "selected_areas": row.get("selected_areas") or payload.get("selected_areas") or [],
        "customers": row.get("customers") or payload.get("customers") or [],
        "objective_ids": payload.get("objective_ids") or ([row.get("objective_id")] if row.get("objective_id") else []),
        "route": row.get("route") or "",
        "remarks": row.get("remarks") or "",
        "joint_working": bool(payload.get("joint_working")),
        "joint_working_seniors": payload.get("joint_working_seniors") or [],
    }


@router.get("/tour-plans/areas")
async def tour_areas(user: dict = Depends(field_user), db: DB = Depends(get_db)):
    return respond("OK", {"items": await _assigned_areas(db, user["employee_id"])})


@router.get("/tour-plans/customers")
async def tour_customers(user: dict = Depends(field_user), db: DB = Depends(get_db), area_id: str = "", q: str = ""):
    del user
    rows = await db.select("healthcare_entities", filters={"status": "active"}, order="name.asc")
    items = []
    for row in rows:
        if q and q.lower() not in (row.get("name") or "").lower():
            continue
        items.append({
            "id": row.get("entity_id"),
            "name": row.get("name"),
            "type": row.get("type"),
            "area_id": area_id,
            "subtitle": row.get("specialty") or row.get("hospital") or "",
        })
    return respond("OK", {"items": items[:80]})


@router.get("/tour-plans")
async def tour_month(user: dict = Depends(field_user), db: DB = Depends(get_db), month: str = ""):
    month = month or date.today().strftime("%Y-%m")
    start, end = month_bounds(month)
    rows = await db.select("tour_plans", filters={"employee_id": user["employee_id"], "plan_date": f"gte.{start}"}, order="plan_date.asc")
    items = []
    for row in rows:
        if not in_month(row.get("plan_date"), start, end):
            continue
        view = _tour_view(row)
        items.append({
            "plan_id": view["plan_id"],
            "plan_date": view["plan_date"],
            "status": view["status"],
            "area_names": [area.get("name") for area in view["selected_areas"] if isinstance(area, dict)],
            "customer_count": len(view["customers"]),
        })
    return respond("OK", {"month": month, "items": items})


@router.get("/tour-plans/{plan_date}")
async def tour_day(plan_date: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    rows = await db.select("tour_plans", filters={"employee_id": user["employee_id"], "plan_date": plan_date}, limit=1)
    if not rows:
        return respond("OK", None)
    return respond("OK", _tour_view(rows[0]))


async def _save_tour(db: DB, employee_id: str, body: dict, status: str) -> dict:
    plan_date = body.get("plan_date") or today_str()
    existing = await db.select("tour_plans", filters={"employee_id": employee_id, "plan_date": plan_date}, limit=1)
    if existing and existing[0].get("status") in ("pending", "approved") and status == "draft":
        raise APIError(409, "This tour plan is locked", "TOUR_PLAN_LOCKED")
    plan_id = (existing[0].get("plan_id") if existing else None) or body.get("plan_id") or new_id("TP")
    objectives = body.get("objective_ids") or []
    payload = {
        "plan_id": plan_id,
        "employee_id": employee_id,
        "plan_date": plan_date,
        "status": status,
        "selected_areas": body.get("selected_areas") or [],
        "customers": body.get("customers") or [],
        "objective_ids": objectives,
        "remarks": body.get("remarks") or "",
        "joint_working": bool(body.get("joint_working")),
        "joint_working_seniors": body.get("joint_working_seniors") or [],
    }
    row = {
        "employee_id": employee_id,
        "plan_date": plan_date,
        "status": status,
        "selected_areas": payload["selected_areas"],
        "customers": payload["customers"],
        "objective_id": ",".join(objectives) if objectives else None,
        "route": body.get("route") or "",
        "remarks": payload["remarks"],
        "payload": payload,
        "updated_at": now_iso(),
    }
    if status == "pending":
        row["submitted_time"] = clock_label()
    if existing:
        await db.update("tour_plans", row, {"plan_id": plan_id})
    else:
        row["plan_id"] = plan_id
        row["created_at"] = now_iso()
        await db.insert("tour_plans", row)
    return {"plan_id": plan_id, "status": status, "plan_date": plan_date}


@router.put("/tour-plans/draft")
async def save_draft(body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    saved = await _save_tour(db, user["employee_id"], body, "draft")
    return respond("Draft saved", {"plan_id": saved["plan_id"], "status": "draft"})


@router.post("/tour-plans/submit")
async def submit_tour(body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    saved = await _save_tour(db, user["employee_id"], body, "pending")
    approval_id = f"APR-{saved['plan_id']}"
    approval = {
        "approval_id": approval_id,
        "employee_id": user["employee_id"],
        "request_type": "tour",
        "request_date": saved["plan_date"],
        "submitted_time": clock_label(),
        "status": "pending",
        "sla": "24h",
        "details": f"Tour plan {saved['plan_date']}",
        "tour_type": "Daily Tour Plan",
    }
    found = await db.select("approval_requests", filters={"approval_id": approval_id}, limit=1)
    if found:
        await db.update("approval_requests", approval, {"approval_id": approval_id})
    else:
        await db.insert("approval_requests", approval)
    return respond("Submitted for approval", {"plan_id": saved["plan_id"], "status": "pending", "approval_id": approval_id})


@router.post("/tour-plans/submit-month")
async def submit_month(body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    month = body.get("month") or date.today().strftime("%Y-%m")
    start, end = month_bounds(month)
    rows = await db.select("tour_plans", filters={"employee_id": user["employee_id"], "plan_date": f"gte.{start}"})
    drafts = [row for row in rows if in_month(row.get("plan_date"), start, end) and row.get("status") in ("draft", "rejected")]
    for row in drafts:
        await db.update("tour_plans", {"status": "pending", "submitted_time": clock_label(), "updated_at": now_iso()}, {"plan_id": row.get("plan_id")})
    approval_id = f"APR-MONTH-{user['employee_id']}-{month}"
    approval = {
        "approval_id": approval_id,
        "employee_id": user["employee_id"],
        "request_type": "tour",
        "request_date": start,
        "submitted_time": clock_label(),
        "status": "pending",
        "sla": "24h",
        "details": f"Monthly tour plan {month}",
        "tour_type": "Monthly Tour Plan",
    }
    found = await db.select("approval_requests", filters={"approval_id": approval_id}, limit=1)
    if found:
        await db.update("approval_requests", approval, {"approval_id": approval_id})
    else:
        await db.insert("approval_requests", approval)
    return respond("Month submitted", {"month": month, "approval_id": approval_id, "status": "pending", "plan_count": len(drafts)})


def _tour_queue_item(row: dict, names: dict[str, dict]) -> dict:
    person = names.get(row.get("employee_id")) or {}
    return {
        "approval_id": row.get("approval_id"),
        "employee_id": row.get("employee_id"),
        "employee_name": person.get("name") or "",
        "month": str(row.get("request_date") or "")[:7],
        "status": row.get("status") or "pending",
        "submitted_at": row.get("applied_at") or row.get("submitted_time"),
        "details": row.get("details") or "",
    }


@router.get("/tour-approvals")
async def tour_approvals(user: dict = Depends(field_user), db: DB = Depends(get_db), status: str = "pending"):
    me = await _user_row(db, user["employee_id"])
    people = await db.select("users")
    team = {
        row.get("employee_id")
        for row in people
        if (row.get("manager") or "").strip().lower() == (me.get("name") or "").strip().lower()
    }
    rows = await db.select("approval_requests", filters={"request_type": "tour"}, order="request_date.desc")
    names = {row.get("employee_id"): row for row in people}
    items = []
    for row in rows:
        if row.get("employee_id") not in team:
            continue
        if status and (row.get("status") or "") != status:
            continue
        items.append(_tour_queue_item(row, names))
    return respond("OK", {"items": items})


@router.get("/tour-approvals/{approval_id}")
async def tour_approval_detail(approval_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("approval_requests", filters={"approval_id": approval_id}, limit=1)
    if not rows:
        raise APIError(404, "Tour approval not found", "NOT_FOUND")
    approval = rows[0]
    names = await _name_map(db)
    person = names.get(approval.get("employee_id")) or {}
    month = str(approval.get("request_date") or "")[:7]
    plans = await db.select("tour_plans", filters={"employee_id": approval.get("employee_id")}, order="plan_date.asc")
    if len(month) == 7:
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


async def _review_tour(db: DB, approval_id: str, status: str, note: str = "") -> dict:
    rows = await db.select("approval_requests", filters={"approval_id": approval_id}, limit=1)
    if not rows:
        raise APIError(404, "Tour approval not found", "NOT_FOUND")
    approval = rows[0]
    updates = {"status": status}
    if note:
        updates["review_note"] = note
    await db.update("approval_requests", updates, {"approval_id": approval_id})
    month = str(approval.get("request_date") or "")[:7]
    plans = await db.select("tour_plans", filters={"employee_id": approval.get("employee_id")})
    updated = 0
    for row in plans:
        if len(month) == 7 and not str(row.get("plan_date") or "").startswith(month):
            if approval.get("tour_type") == "Monthly Tour Plan":
                continue
            if row.get("plan_id") and approval_id != f"APR-{row.get('plan_id')}":
                continue
        await db.update("tour_plans", {"status": status, "updated_at": now_iso()}, {"plan_id": row.get("plan_id")})
        updated += 1
    return {"approval_id": approval_id, "status": status, "review_note": note, "plans_updated": updated}


@router.post("/tour-approvals/{approval_id}/approve")
async def approve_tour(approval_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    data = await _review_tour(db, approval_id, "approved")
    return respond("Tour plan approved", data)


@router.post("/tour-approvals/{approval_id}/reject")
async def reject_tour(approval_id: str, body: dict = Body(default={}), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    data = await _review_tour(db, approval_id, "rejected", str(body.get("review_note") or ""))
    return respond("Tour plan rejected", data)


# --- POB, products, cabinet ----------------------------------------------

@router.get("/pob/form")
async def pob_form(user: dict = Depends(field_user), db: DB = Depends(get_db)):
    me = await _user_row(db, user["employee_id"])
    territory = me.get("territory") or ""
    entities = await db.select("healthcare_entities", filters={"status": "active"}, order="name.asc")
    contacts = []
    stockists = []
    for row in entities:
        if territory and row.get("territory") and row.get("territory") != territory:
            continue
        if row.get("type") == "stockist":
            stockists.append({"id": row.get("entity_id"), "name": row.get("name")})
        else:
            contacts.append({"id": row.get("entity_id"), "name": row.get("name"), "type": row.get("type"), "phone": row.get("phone") or ""})
    products = await db.select("products", filters={"status": "active"}, order="name.asc")
    return respond("OK", {
        "contacts": contacts[:100],
        "stockists": stockists,
        "products": [{"id": row.get("product_id"), "name": row.get("name"), "mrp": row.get("mrp")} for row in products],
    })


@router.post("/pob/orders")
async def submit_pob(body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    items = [item for item in (body.get("items") or []) if int(num(item.get("quantity"))) > 0]
    if not items:
        raise APIError(422, "Add at least one product", "VALIDATION_ERROR")
    order_id = new_id("POB")
    total = 0.0
    line_rows = []
    for index, item in enumerate(items, start=1):
        qty = int(num(item.get("quantity")))
        rate = num(item.get("rate") or item.get("mrp"))
        line = qty * rate
        total += line
        line_rows.append({
            "item_id": f"{order_id}-{index}",
            "order_id": order_id,
            "product_id": item.get("product_id") or item.get("id") or "",
            "product_name": item.get("product_name") or item.get("name") or "",
            "quantity": qty,
            "rate": rate,
            "mrp": num(item.get("mrp") or rate),
            "line_total": line,
        })
    await db.insert("pob_orders", {
        "order_id": order_id,
        "employee_id": user["employee_id"],
        "pv_type": body.get("pv_type") or "",
        "contact_name": body.get("contact_name") or "",
        "contact_id": body.get("contact_id") or "",
        "phone": body.get("phone") or "",
        "stockist_name": body.get("stockist_name") or "",
        "total_amount": total,
        "item_count": len(line_rows),
        "status": "submitted",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    })
    await db.insert("pob_order_items", line_rows)
    return respond("Order submitted", {"order_id": order_id, "status": "submitted", "total_amount": total, "item_count": len(line_rows)}, 201)


@router.get("/pob/orders")
async def my_orders(user: dict = Depends(field_user), db: DB = Depends(get_db), month: str = ""):
    me = await _user_row(db, user["employee_id"])
    rows = await db.select("pob_orders", filters={"employee_id": user["employee_id"]}, order="created_at.desc")
    if month:
        start, end = month_bounds(month)
        rows = [row for row in rows if in_month(row.get("created_at"), start, end)]
    return respond("OK", {"items": [_order_summary(row, me.get("name") or "", me.get("territory") or "") for row in rows]})


@router.get("/pob/orders/{order_id}")
async def order_detail(order_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    rows = await db.select("pob_orders", filters={"order_id": order_id}, limit=1)
    if not rows or rows[0].get("employee_id") != user["employee_id"]:
        raise APIError(404, "Order not found", "NOT_FOUND")
    items = await db.select("pob_order_items", filters={"order_id": order_id})
    order = rows[0]
    return respond("OK", {
        "order_id": order_id,
        "status": order.get("status"),
        "total_amount": num(order.get("total_amount")),
        "contact_name": order.get("contact_name"),
        "stockist_name": order.get("stockist_name"),
        "items": [
            {
                "product_name": item.get("product_name"),
                "quantity": item.get("quantity"),
                "rate": num(item.get("rate")),
                "line_total": num(item.get("line_total")),
            }
            for item in items
        ],
    })


@router.get("/products/assigned")
async def assigned_products(user: dict = Depends(field_user), db: DB = Depends(get_db), period: str = ""):
    period = period or period_label()
    targets = await db.select("sales_targets", filters={"employee_id": user["employee_id"], "period": period}, limit=1)
    wanted = set()
    for item in (targets[0].get("products") if targets else []) or []:
        if isinstance(item, dict):
            wanted.add(item.get("product_id") or item.get("name"))
    rows = await db.select("products", filters={"status": "active"}, order="name.asc")
    items = [product_card(row) for row in rows if not wanted or row.get("product_id") in wanted or row.get("name") in wanted]
    page_items, meta = paginate(items, 1, 100)
    return respond("OK", {"total_records": len(items), "items": page_items, "pagination": meta})


@router.get("/products")
async def products(user: dict = Depends(field_user), db: DB = Depends(get_db), q: str = "", page: int = 1, limit: int = 20):
    del user
    rows = await db.select("products", filters={"status": "active"}, order="name.asc")
    items = [product_card(row) for row in rows if not q or q.lower() in (row.get("name") or "").lower()]
    page_items, meta = paginate(items, page, limit)
    return respond("OK", {"total_records": len(items), "items": page_items, "pagination": meta})


async def _folders(db: DB) -> tuple[list[dict], list[dict]]:
    folders = await db.select("cabinet_folders", order="label.asc")
    files = await db.select("cabinet_files", order="name.asc")
    return folders, files


def _folder_tree(folders: list[dict], files: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for file in files:
        if file.get("is_active", True):
            counts[file.get("folder_id")] = counts.get(file.get("folder_id"), 0) + 1
    nodes = {row.get("folder_id"): _folder_card(row, counts.get(row.get("folder_id"), 0)) for row in folders}
    roots = []
    for row in folders:
        node = nodes[row.get("folder_id")]
        parent = row.get("parent_id")
        if parent and parent in nodes:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)
    return roots


@router.get("/cabinet/folders")
async def cabinet_folders(user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    folders, files = await _folders(db)
    active = [file for file in files if file.get("is_active", True)]
    return respond("OK", {"items": _folder_tree(folders, active)})


@router.get("/cabinet/search")
async def cabinet_search(user: dict = Depends(field_user), db: DB = Depends(get_db), q: str = ""):
    del user
    folders, files = await _folders(db)
    names = {row.get("folder_id"): row.get("label") for row in folders}
    needle = q.lower()
    folder_hits = [_folder_card(row) for row in folders if needle in (row.get("label") or "").lower()]
    file_hits = [
        _file_card(row, names.get(row.get("folder_id"), ""))
        for row in files
        if row.get("is_active", True) and (not needle or needle in (row.get("name") or "").lower())
    ]
    return respond("OK", {"folders": folder_hits, "files": file_hits})


@router.get("/cabinet/folders/{folder_id}/files")
async def cabinet_files(folder_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    folders = await db.select("cabinet_folders", filters={"folder_id": folder_id}, limit=1)
    if not folders:
        raise APIError(404, "Folder not found", "NOT_FOUND")
    files = await db.select("cabinet_files", filters={"folder_id": folder_id}, order="name.asc")
    active = [row for row in files if row.get("is_active", True)]
    return respond("OK", {"folder": _folder_card(folders[0], len(active)), "items": [_file_card(row) for row in active]})


@router.get("/cabinet/files/{file_id}/preview")
async def cabinet_preview(file_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    rows = await db.select("cabinet_files", filters={"file_id": file_id}, limit=1)
    if not rows:
        raise APIError(404, "File not found", "NOT_FOUND")
    path = rows[0].get("storage_path") or ""
    url = rows[0].get("file_url") or ""
    if path:
        url = await db.signed_url("cabinet-files", path, 7200)
    return respond("OK", {"file_id": file_id, "name": rows[0].get("name"), "preview_url": url})


# --- leaves, expenses, notifications -------------------------------------

def _leave_item(row: dict) -> dict:
    return {
        "approval_id": row.get("approval_id"),
        "employee_id": row.get("employee_id"),
        "leave_type": row.get("leave_type") or "",
        "from_date": as_date(row.get("leave_from")),
        "to_date": as_date(row.get("leave_to")),
        "days": row.get("leave_days") or 0,
        "reason": row.get("leave_reason") or "",
        "status": row.get("status") or "pending",
        "review_note": row.get("review_note") or "",
        "submitted_at": row.get("applied_at"),
    }


async def _balance(db: DB, employee_id: str) -> dict:
    rows = await db.select("leave_balances", filters={"employee_id": employee_id}, limit=1)
    if not rows:
        created = {
            "employee_id": employee_id,
            "casual_leave": 8,
            "earned_leave": 12,
            "medical_leave": 6,
            "compensatory_leave": 2,
            "casual_leave_used": 0,
            "earned_leave_used": 0,
            "medical_leave_used": 0,
            "compensatory_leave_used": 0,
        }
        await db.insert("leave_balances", created)
        return created
    return rows[0]


@router.get("/leaves")
async def leave_dashboard(user: dict = Depends(field_user), db: DB = Depends(get_db)):
    balance = await _balance(db, user["employee_id"])
    rows = await db.select("approval_requests", filters={"employee_id": user["employee_id"], "request_type": "leave"}, order="applied_at.desc")
    return respond("OK", {
        "balance": {
            "casual_leave": num(balance.get("casual_leave")),
            "casual_leave_used": num(balance.get("casual_leave_used")),
            "earned_leave": num(balance.get("earned_leave")),
            "earned_leave_used": num(balance.get("earned_leave_used")),
            "medical_leave": num(balance.get("medical_leave")),
            "medical_leave_used": num(balance.get("medical_leave_used")),
            "compensatory_leave": num(balance.get("compensatory_leave")),
            "compensatory_leave_used": num(balance.get("compensatory_leave_used")),
        },
        "requests": [_leave_item(row) for row in rows],
    })


@router.post("/leaves")
async def apply_leave(body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    leave_type = body.get("leave_type") or "casual_leave"
    start = body.get("from_date") or today_str()
    end = body.get("to_date") or start
    days = int(num(body.get("days"))) or (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    balance = await _balance(db, user["employee_id"])
    column = {
        "casual_leave": "casual_leave",
        "earned_leave": "earned_leave",
        "medical_leave": "medical_leave",
        "compensatory_leave": "compensatory_leave",
    }.get(leave_type, "casual_leave")
    remaining = num(balance.get(column)) - num(balance.get(f"{column}_used"))
    if days > remaining:
        raise APIError(409, "Not enough leave balance", "INSUFFICIENT_BALANCE")
    existing = await db.select("approval_requests", filters={"employee_id": user["employee_id"], "request_type": "leave"})
    for row in existing:
        if row.get("status") not in ("pending", "approved"):
            continue
        other_from = as_date(row.get("leave_from"))
        other_to = as_date(row.get("leave_to")) or other_from
        if other_from and not (end < other_from or start > other_to):
            raise APIError(409, "Leave dates overlap an existing request", "OVERLAP")
    approval_id = new_id("APR")
    await db.insert("approval_requests", {
        "approval_id": approval_id,
        "employee_id": user["employee_id"],
        "request_type": "leave",
        "request_date": today_str(),
        "submitted_time": clock_label(),
        "status": "pending",
        "sla": "—",
        "details": f"{leave_type} · {body.get('reason') or ''}",
        "leave_from": start,
        "leave_to": end,
        "leave_days": days,
        "leave_reason": body.get("reason") or "",
        "leave_type": leave_type,
        "applied_at": now_iso(),
        "updated_at": now_iso(),
    })
    return respond("Leave submitted", {
        "approval_id": approval_id,
        "status": "pending",
        "leave_type": leave_type,
        "from_date": start,
        "to_date": end,
        "days": days,
    }, 201)


async def _review_leave(db: DB, approval_id: str, status: str, note: str = "") -> dict:
    rows = await db.select("approval_requests", filters={"approval_id": approval_id}, limit=1)
    if not rows or rows[0].get("request_type") != "leave":
        raise APIError(404, "Leave request not found", "NOT_FOUND")
    row = rows[0]
    await db.update("approval_requests", {"status": status, "review_note": note, "updated_at": now_iso()}, {"approval_id": approval_id})
    balance_view = None
    if status == "approved" and row.get("status") != "approved":
        leave_type = row.get("leave_type") or "casual_leave"
        used_col = f"{leave_type}_used" if leave_type.endswith("_leave") else "casual_leave_used"
        if not used_col.endswith("_used"):
            used_col = "casual_leave_used"
        balance = await _balance(db, row.get("employee_id"))
        new_used = num(balance.get(used_col)) + num(row.get("leave_days"))
        await db.update("leave_balances", {used_col: new_used}, {"employee_id": row.get("employee_id")})
        balance_view = {leave_type: num(balance.get(leave_type)), used_col: new_used}
    return {"approval_id": approval_id, "status": status, "review_note": note, "balance": balance_view}


@router.post("/leaves/{approval_id}/approve")
async def approve_leave(approval_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    return respond("Leave approved", await _review_leave(db, approval_id, "approved"))


@router.post("/leaves/{approval_id}/reject")
async def reject_leave(approval_id: str, body: dict = Body(default={}), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    return respond("Leave rejected", await _review_leave(db, approval_id, "rejected", str(body.get("review_note") or "")))


@router.get("/expenses")
async def list_expenses(user: dict = Depends(field_user), db: DB = Depends(get_db), month: str = ""):
    rows = await db.select("expense_claims", filters={"employee_id": user["employee_id"]}, order="claim_date.desc")
    if month:
        start, end = month_bounds(month)
        rows = [row for row in rows if in_month(row.get("claim_date"), start, end)]
    return respond("OK", {"items": [_claim(row) for row in rows]})


@router.get("/expenses/by-date/{claim_date}")
async def expense_by_date(claim_date: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    rows = await db.select("expense_claims", filters={"employee_id": user["employee_id"], "claim_date": claim_date}, limit=1)
    return respond("OK", None if not rows else _claim(rows[0]))


@router.post("/expenses")
async def submit_expense(body: dict = Body(...), user: dict = Depends(field_user), db: DB = Depends(get_db)):
    claim_date = body.get("claim_date") or today_str()
    existing = await db.select("expense_claims", filters={"employee_id": user["employee_id"], "claim_date": claim_date}, limit=1)
    if existing and existing[0].get("status") == "approved":
        raise APIError(409, "Approved claims cannot be edited", "CLAIM_LOCKED")
    misc = num(body.get("miscellaneous"))
    phone = num(body.get("phone_allowance"))
    internet = num(body.get("internet_reimbursement"))
    total = misc + phone + internet
    payload = {
        "claim_date": claim_date,
        "status": "pending",
        "miscellaneous": misc,
        "phone_allowance": phone,
        "internet_reimbursement": internet,
        "total_amount": total,
        "comment": body.get("comment") or "",
        "review_note": "",
        "submitted_at": now_iso(),
        "updated_at": now_iso(),
    }
    if existing:
        claim_id = existing[0].get("claim_id")
        await db.update("expense_claims", payload, {"claim_id": claim_id})
    else:
        claim_id = body.get("claim_id") or new_id("EXP")
        payload["claim_id"] = claim_id
        payload["employee_id"] = user["employee_id"]
        await db.insert("expense_claims", payload)
    return respond("Claim submitted", {"claim_id": claim_id, "status": "pending"}, 201)


def _note_card(row: dict) -> dict:
    created = row.get("created_at") or now_iso()
    return {
        "id": row.get("notification_id") or row.get("id"),
        "title": row.get("title") or "",
        "description": row.get("description") or row.get("body") or "",
        "time_ago": "",
        "created_at": created,
        "type": row.get("type") or "report",
        "is_read": bool(row.get("is_read")),
        "action_label": row.get("action_label"),
        "action_url": row.get("action_url"),
    }


@router.get("/notifications")
async def notifications(user: dict = Depends(field_user), db: DB = Depends(get_db)):
    rows = await db.select("user_notifications", filters={"employee_id": user["employee_id"]}, order="created_at.desc")
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    today_items, older = [], []
    for row in rows:
        card = _note_card(row)
        (today_items if str(card["created_at"]).startswith(today) else older).append(card)
    yesterday_items = [item for item in older if str(item["created_at"]).startswith(yesterday)]
    rest = [item for item in older if item not in yesterday_items]
    return respond("OK", {
        "unread_count": sum(1 for row in rows if not row.get("is_read")),
        "today": today_items,
        "yesterday": yesterday_items + rest,
    })


@router.post("/notifications/read-all")
async def read_all(user: dict = Depends(field_user), db: DB = Depends(get_db)):
    rows = await db.select("user_notifications", filters={"employee_id": user["employee_id"], "is_read": "eq.false"})
    for row in rows:
        key = row.get("notification_id")
        if key:
            await db.update("user_notifications", {"is_read": True}, {"notification_id": key})
    return respond("Marked read", {"unread_count": 0})


@router.patch("/notifications/{notification_id}/read")
async def read_one(notification_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    saved = await db.update("user_notifications", {"is_read": True}, {"notification_id": notification_id})
    if saved and saved[0].get("employee_id") not in (None, user["employee_id"]):
        raise APIError(403, "You cannot update this notification", "FORBIDDEN")
    unread = await db.select("user_notifications", filters={"employee_id": user["employee_id"], "is_read": "eq.false"})
    return respond("Marked read", {"id": notification_id, "is_read": True, "unread_count": len(unread)})


@router.delete("/notifications/{notification_id}")
async def dismiss_notification(notification_id: str, user: dict = Depends(field_user), db: DB = Depends(get_db)):
    del user
    await db.delete("user_notifications", {"notification_id": notification_id})
    return respond("Dismissed", None)
