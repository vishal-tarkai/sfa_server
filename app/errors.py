"""JSON error envelope shared by every route."""

from __future__ import annotations

from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        code: str,
        errors: dict | None = None,
    ) -> None:
        self.status = status
        self.message = message
        self.code = code
        self.errors = errors
        super().__init__(message)


def respond(message: str, data=None, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"success": True, "message": message, "data": data},
        status_code=status,
    )


def fail_body(message: str, code: str, errors: dict | None = None) -> dict:
    body = {"success": False, "message": message, "error_code": code}
    if errors:
        body["errors"] = errors
    return body
