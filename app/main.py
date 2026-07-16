"""FastAPI entrypoint: wiring, startup gate, error envelope."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import assert_prod_ready, get_settings
from .db import SessionLocal, init_db
from .envelope import ApiError
from .routers import ai, billing, config, content, peruser, user
from .seed import seed_all

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    assert_prod_ready(settings)   # refuse insecure prod boot BEFORE serving traffic
    init_db()
    db = SessionLocal()
    try:
        seed_all(db)
    finally:
        db.close()
    yield


app = FastAPI(title="Tinh Hoa Sách — Backend", version="1.0.0", lifespan=lifespan)


# ── error envelope (spec §9) ─────────────────────────────────────────
@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError):
    if exc.raw is not None:
        return JSONResponse(status_code=exc.code, content=exc.raw)
    return JSONResponse(status_code=exc.code,
                        content={"status": {"code": exc.code, "message": exc.message}})


@app.exception_handler(RequestValidationError)
async def _validation_handler(_: Request, exc: RequestValidationError):
    # Keep only JSON-safe fields (pydantic v2 ctx can hold exception objects).
    errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")}
              for e in exc.errors()]
    return JSONResponse(status_code=400,
                        content={"status": {"code": 400, "message": "Bad request"},
                                 "errors": errors})


# ── routers ──────────────────────────────────────────────────────────
app.include_router(config.router)
app.include_router(user.router)
app.include_router(content.router)
app.include_router(peruser.router)
app.include_router(billing.router)
app.include_router(ai.router)


@app.get("/health")
def health():
    return {"status": {"code": 200, "message": "OK"},
            "prod_verify": settings.prod_verify_enabled,
            "rtdn_audience_set": settings.rtdn_audience_set}


@app.get("/")
def root():
    return {"status": {"code": 200, "message": "Tinh Hoa Sách backend v1"}}
