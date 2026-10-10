"""Supabase PostgREST and Storage client used by the API."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from app.config import SUPABASE_ANON_KEY, SUPABASE_URL
from app.errors import APIError


class DB:
    def __init__(self, url: str, key: str) -> None:
        self.url = url.rstrip("/")
        self.key = key
        self.rest = f"{self.url}/rest/v1"
        self.storage = f"{self.url}/storage/v1"
        self.client = httpx.AsyncClient(timeout=40, trust_env=False)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.key and "your-project" not in self.url)

    def _headers(self, prefer: str | None = None, content_type: str | None = "application/json") -> dict:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _ensure(self) -> None:
        if not self.configured:
            raise APIError(
                503,
                "Supabase is not configured. Set SUPABASE_URL and SUPABASE_ANON_KEY in CMS/.env.",
                "SERVICE_UNAVAILABLE",
            )

    def _raise(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        text = response.text[:800]
        code = "SERVICE_UNAVAILABLE"
        status = 502
        try:
            payload = response.json()
            pg = str(payload.get("code") or "")
            message = payload.get("message") or payload.get("error") or text
        except Exception:
            pg = ""
            message = text or "Database request failed"
        if pg == "23505" or "duplicate" in str(message).lower():
            status = 409
            code = "CONFLICT"
        elif response.status_code == 404:
            status = 404
            code = "NOT_FOUND"
        raise APIError(status, str(message), code)

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise APIError(503, "Could not reach the database. Please try again.", "SERVICE_UNAVAILABLE") from exc
        self._raise(response)
        return response

    async def select(
        self,
        table: str,
        *,
        filters: dict[str, Any] | None = None,
        order: str | None = None,
        limit: int | None = None,
        select: str = "*",
    ) -> list[dict]:
        self._ensure()
        params: dict[str, str] = {"select": select}
        _ops = ("eq.", "neq.", "gt.", "gte.", "lt.", "lte.", "like.", "ilike.", "in.", "is.")
        for key, value in (filters or {}).items():
            if value is None:
                continue
            if isinstance(value, str) and any(value.startswith(op) for op in _ops):
                params[key] = value
            else:
                params[key] = f"eq.{value}"
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)
        response = await self._request(
            "GET",
            f"{self.rest}/{table}",
            params=params,
            headers=self._headers(content_type=None),
        )
        data = response.json()
        if isinstance(data, list):
            return data
        return [data] if data else []

    async def insert(self, table: str, body: dict | list) -> list[dict]:
        self._ensure()
        response = await self._request(
            "POST",
            f"{self.rest}/{table}",
            content=json.dumps(body),
            headers=self._headers("return=representation"),
        )
        data = response.json()
        if isinstance(data, list):
            return data
        return [data] if data else []

    async def update(self, table: str, body: dict, filters: dict[str, Any]) -> list[dict]:
        self._ensure()
        params = {key: f"eq.{value}" for key, value in filters.items()}
        response = await self._request(
            "PATCH",
            f"{self.rest}/{table}",
            params=params,
            content=json.dumps(body),
            headers=self._headers("return=representation"),
        )
        data = response.json()
        if isinstance(data, list):
            return data
        return [data] if data else []

    async def upsert(self, table: str, body: dict | list, on_conflict: str) -> list[dict]:
        self._ensure()
        response = await self._request(
            "POST",
            f"{self.rest}/{table}",
            params={"on_conflict": on_conflict},
            content=json.dumps(body),
            headers=self._headers("resolution=merge-duplicates,return=representation"),
        )
        data = response.json()
        if isinstance(data, list):
            return data
        return [data] if data else []

    async def delete(self, table: str, filters: dict[str, Any]) -> None:
        self._ensure()
        params = {}
        for key, value in filters.items():
            if isinstance(value, str) and value.startswith("in."):
                params[key] = value
            else:
                params[key] = f"eq.{value}"
        await self._request(
            "DELETE",
            f"{self.rest}/{table}",
            params=params,
            headers=self._headers(content_type=None),
        )

    async def upload(self, bucket: str, path: str, data: bytes, content_type: str) -> str:
        self._ensure()
        safe = "/".join(quote(part) for part in path.split("/"))
        await self._request(
            "POST",
            f"{self.storage}/object/{bucket}/{safe}",
            content=data,
            headers={
                **self._headers(content_type=content_type),
                "x-upsert": "true",
            },
        )
        return f"{self.storage}/object/public/{bucket}/{safe}"

    async def signed_url(self, bucket: str, path: str, expires_in: int = 7200) -> str:
        self._ensure()
        safe = "/".join(quote(part) for part in path.split("/"))
        response = await self.client.post(
            f"{self.storage}/object/sign/{bucket}/{safe}",
            content=json.dumps({"expiresIn": expires_in}),
            headers=self._headers(),
        )
        if response.status_code >= 400:
            return f"{self.storage}/object/public/{bucket}/{safe}"
        payload = response.json()
        signed = payload.get("signedURL") or payload.get("signedUrl") or ""
        if signed.startswith("http"):
            return signed
        if signed.startswith("/"):
            return f"{self.url}{signed}" if not signed.startswith("/storage") else f"{self.url}/storage/v1{signed}"
        return f"{self.storage}/object/public/{bucket}/{safe}"

    async def remove_object(self, bucket: str, path: str) -> None:
        self._ensure()
        try:
            response = await self.client.request(
                "DELETE",
                f"{self.storage}/object/{bucket}",
                content=json.dumps({"prefixes": [path]}),
                headers=self._headers(),
            )
        except httpx.HTTPError:
            return
        if response.status_code >= 400:
            return

    async def close(self) -> None:
        await self.client.aclose()


def create_db() -> DB:
    return DB(SUPABASE_URL, SUPABASE_ANON_KEY)
