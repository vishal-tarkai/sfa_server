"""Shared mapping and date helpers."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_str() -> str:
    return date.today().isoformat()


def clock_label(moment: datetime | None = None) -> str:
    moment = moment or datetime.now()
    return moment.strftime("%I:%M %p").lstrip("0")


def period_label(moment: date | None = None) -> str:
    moment = moment or date.today()
    return f"{MONTHS[moment.month - 1]} {moment.year}"


def month_bounds(month: str) -> tuple[str, str]:
    """month is YYYY-MM."""
    year, mon = month.split("-")
    start = date(int(year), int(mon), 1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    from datetime import timedelta
    last = end - timedelta(days=1)
    return start.isoformat(), last.isoformat()


def normalize_mobile(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) > 10 else digits


def mobile_ok(value: str) -> bool:
    return bool(re.fullmatch(r"[6-9]\d{9}", normalize_mobile(value)))


def display_mobile(raw: str, digits: str) -> str:
    text = (raw or "").strip()
    if text:
        return text
    if len(digits) == 10:
        return f"+91 {digits[:5]} {digits[5:]}"
    return digits


def initials(name: str) -> str:
    parts = [part for part in (name or "").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:1].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


def public_user(row: dict) -> dict:
    mobile = normalize_mobile(str(row.get("mobile") or ""))
    embedding = row.get("face_embedding")
    return {
        "employee_id": row.get("employee_id") or "",
        "name": row.get("name") or "",
        "avatar": row.get("avatar") or initials(row.get("name") or ""),
        "role": row.get("role") or "",
        "designation": row.get("designation") or "",
        "territory": row.get("territory") or "",
        "org_node_id": row.get("org_node_id") or "",
        "location": row.get("territory") or "",
        "mobile": mobile,
        "mobile_display": display_mobile(str(row.get("mobile") or ""), mobile),
        "email": row.get("email") or "",
        "manager": row.get("manager") or "",
        "join_date": row.get("join_date") or "",
        "status": row.get("status") or "",
        "last_login": row.get("last_login") or "",
        "profile_image_url": row.get("profile_image_url") or "",
        "biometric_enrolled": bool(row.get("biometric")) or bool(embedding),
    }


def login_user(row: dict) -> dict:
    user = public_user(row)
    return {
        "employee_id": user["employee_id"],
        "name": user["name"],
        "avatar": user["avatar"],
        "role": user["role"],
        "designation": user["designation"],
        "territory": user["territory"],
        "location": user["location"],
        "mobile": user["mobile"],
        "mobile_display": user["mobile_display"],
        "email": user["email"],
        "manager": user["manager"],
        "join_date": user["join_date"],
        "status": user["status"],
        "last_login": user["last_login"],
        "profile_image_url": user["profile_image_url"],
    }


def profile_user(row: dict, target_percent: int = 0) -> dict:
    user = public_user(row)
    return {
        "id": user["employee_id"],
        "employee_id": user["employee_id"],
        "full_name": user["name"],
        "role": user["role"],
        "designation": user["designation"],
        "location": user["location"],
        "territory": user["territory"],
        "work_email": user["email"],
        "phone_number": user["mobile_display"],
        "profile_image_url": user["profile_image_url"],
        "target_met_percent": target_percent,
        "last_synced": now_iso(),
        "visit_reminders_enabled": bool(row.get("visit_reminders_enabled", True)),
        "biometric_enrolled": user["biometric_enrolled"],
    }


def norm_entity_type(value: str | None) -> str:
    return (value or "").strip().lower()


def entity_visible_for_territory(
    row_territory: str | None,
    filter_territory: str | None,
    *,
    employee_id: str = "",
    assigned_employee_id: str | None = None,
) -> bool:
    """Territory match (case-insensitive) or entity assigned to this MR."""
    if not filter_territory or not filter_territory.strip():
        return True
    row_t = (row_territory or "").strip().casefold()
    want = filter_territory.strip().casefold()
    if row_t == want:
        return True
    assigned = (assigned_employee_id or "").strip()
    return bool(assigned and employee_id and assigned == employee_id)


def period_sort_key(label: str) -> tuple[int, int]:
    """Sort key for labels like `June 2026` (newest first when reversed)."""
    parts = (label or "").strip().split()
    if len(parts) < 2:
        return (0, 0)
    try:
        year = int(parts[-1])
    except ValueError:
        year = 0
    token = parts[0].lower()
    month = 1
    for index, name in enumerate(MONTHS, start=1):
        if name.lower().startswith(token[:3]):
            month = index
            break
    return (year, month)


def pick_sales_target_row(rows: list[dict], period: str) -> tuple[dict | None, str]:
    """Prefer the requested period when it has products; else latest period with products."""
    if not rows:
        return None, period

    current = next((row for row in rows if row.get("period") == period), None)
    if current is not None and sales_target_product_items(current):
        return current, period

    with_products = [row for row in rows if sales_target_product_items(row)]
    if with_products:
        with_products.sort(
            key=lambda row: period_sort_key(row.get("period") or ""),
            reverse=True,
        )
        best = with_products[0]
        return best, (best.get("period") or period)

    if current is not None:
        return current, period

    published = [row for row in rows if (row.get("status") or "").lower() == "published"]
    fallback = published[-1] if published else rows[-1]
    return fallback, (fallback.get("period") or period)


def sales_target_product_items(row: dict | None) -> list[dict]:
    """`products` may be TEXT[] names or list of product objects."""
    if not row:
        return []
    breakdown = row.get("product_breakdown") or {}
    if not isinstance(breakdown, dict):
        breakdown = {}
    items: list[dict] = []
    seen: set[str] = set()
    for entry in row.get("products") or []:
        if isinstance(entry, dict):
            name = (entry.get("name") or entry.get("product_name") or "").strip()
            if not name:
                continue
            target = int(num(entry.get("target_amount") or entry.get("target_inr")))
            achieved = int(num(entry.get("achieved_amount") or entry.get("achieved_inr")))
            pct = int(num(entry.get("achievement_percent")))
        elif isinstance(entry, str):
            name = entry.strip()
            if not name:
                continue
            target = int(num(breakdown.get(name)))
            achieved = 0
            pct = 0
        else:
            continue
        if name in seen:
            continue
        seen.add(name)
        items.append({
            "name": name,
            "target_inr": target,
            "achieved_inr": achieved,
            "achievement_percent": pct,
        })
    for name, amount in breakdown.items():
        key = str(name).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        items.append({
            "name": key,
            "target_inr": int(num(amount)),
            "achieved_inr": 0,
            "achievement_percent": 0,
        })
    return items


def product_name_matches_assignment(product_name: str, assigned_name: str) -> bool:
    product = (product_name or "").strip().lower()
    assigned = (assigned_name or "").strip().lower()
    if not product or not assigned:
        return False
    if product == assigned or assigned in product or product in assigned:
        return True
    assigned_first = assigned.split()[0]
    product_first = product.split()[0]
    return product.startswith(assigned_first) or assigned.startswith(product_first)


def entity_card(row: dict, stockist_name: str = "") -> dict:
    specialty = row.get("specialty") or ""
    hospital = row.get("hospital") or ""
    subtitle = " · ".join(part for part in (specialty, hospital) if part)
    raw_type = row.get("type") or ""
    return {
        "id": row.get("entity_id") or row.get("id") or "",
        "entity_id": row.get("entity_id") or "",
        "name": row.get("name") or "",
        "subtitle": subtitle,
        "type": norm_entity_type(raw_type) or raw_type,
        "status": row.get("status") or "active",
        "address": row.get("address") or row.get("city") or "",
        "phone": row.get("phone") or "",
        "territory": row.get("territory") or "",
        "city": row.get("city") or "",
        "hospital": hospital,
        "specialty": specialty,
        "tier": row.get("tier") or "",
        "calls": row.get("calls") or 0,
        "rx": row.get("rx") or "",
        "dob": row.get("dob") or "",
        "anniversary": row.get("anniversary") or "",
        "stockist_id": row.get("stockist_id") or "",
        "stockist_name": stockist_name,
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
    }


def product_card(row: dict) -> dict:
    return {
        "id": row.get("product_id") or "",
        "product_id": row.get("product_id") or "",
        "name": row.get("name") or "",
        "subtitle": row.get("subtitle") or "",
        "mono_packing": row.get("mono_packing") or "",
        "shipper_qty": row.get("shipper_qty"),
        "mrp": row.get("mrp"),
        "is_new": bool(row.get("is_new")),
        "subtitle_color": row.get("subtitle_color") or "#6B7280",
        "image_url": row.get("image_url") or "",
        "status": row.get("status") or "active",
        "updated_at": row.get("updated_at"),
    }


def area_card(row: dict) -> dict:
    return {
        "id": row.get("node_id") or row.get("id") or "",
        "name": row.get("label") or row.get("name") or "",
        "node_type": row.get("node_type") or "area",
        "parent_name": row.get("parent_name"),
        "city_name": row.get("city_name"),
    }


def paginate(items: list, page: int, limit: int) -> tuple[list, dict]:
    page = max(1, page or 1)
    limit = min(100, max(1, limit or 20))
    total = len(items)
    start = (page - 1) * limit
    pages = max(1, (total + limit - 1) // limit) if total else 1
    return items[start : start + limit], {
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": pages,
    }


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def as_date(value: Any) -> str:
    text = str(value or "")[:10]
    return text


def in_month(value: Any, start: str, end: str) -> bool:
    text = as_date(value)
    return bool(text) and start <= text <= end


def num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def new_id(prefix: str) -> str:
    return f"{prefix}{int(datetime.now().timestamp() * 1000)}"
