"""SPINE SFA API.

Run from the server directory:

    .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload

Swagger UI: /docs  or  /swagger
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, RedirectResponse

from app.db import create_db
from app.errors import APIError, fail_body
from app.routers.admin import router as admin_router
from app.routers.field import router as field_router
from app.routers.ops import router as ops_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = create_db()
    yield
    await app.state.db.close()


app = FastAPI(
    title="SPINE SFA API",
    version="1.0.0",
    description=(
        "Field app and admin REST API.\n\n"
        "**Test in Swagger**\n"
        "1. Open `/docs` or `/swagger`\n"
        "2. Field login: `POST /v1/auth/login` with mobile + password\n"
        "3. Admin login: `POST /v1/admin/auth/login` with email + password\n"
        "4. Click **Authorize**, paste `access_token` (no Bearer prefix needed)\n"
        "5. Try authenticated endpoints"
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    swagger_ui_parameters={
        "persistAuthorization": True,
        "displayRequestDuration": True,
        "tryItOutEnabled": True,
        "filter": True,
    },
    openapi_tags=[
        {"name": "meta", "description": "Health and docs"},
        {"name": "field", "description": "CMS / MR field app APIs (auth, attendance, DPR, tour, POB, leaves, expenses)"},
        {"name": "admin", "description": "Admin console APIs (users, org, products, healthcare, approvals, cabinet, head offices, notifications, audit)"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(APIError)
async def handle_api_error(_: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(fail_body(exc.message, exc.code, exc.errors), status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        fail_body("Validation failed", "VALIDATION_ERROR", {"detail": [err.get("msg", "") for err in exc.errors()]}),
        status_code=422,
    )


app.include_router(field_router, prefix="/v1")
app.include_router(admin_router, prefix="/v1")
app.include_router(ops_router, prefix="/v1")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/swagger", include_in_schema=False)
async def swagger_alias() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/swagger-ui", include_in_schema=False)
async def swagger_ui() -> object:
    return get_swagger_ui_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title="SPINE SFA API — Swagger",
        swagger_ui_parameters=app.swagger_ui_parameters,
    )


@app.get("/v1/health", tags=["meta"])
async def health(request: Request) -> dict:
    db = request.app.state.db
    return {
        "success": True,
        "message": "OK",
        "data": {
            "configured": db.configured,
            "docs": "/docs",
            "swagger": "/swagger",
            "redoc": "/redoc",
        },
    }
