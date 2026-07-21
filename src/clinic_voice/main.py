from __future__ import annotations

import json
import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from clinic_voice.api import router
from clinic_voice.config import get_settings
from clinic_voice.database import SessionLocal, create_schema
from clinic_voice.errors import DomainError
from clinic_voice.models import ToolAuditLog

settings = get_settings()
logging.basicConfig(level=settings.log_level)
app = FastAPI(
    title="Clinic Voice Receptionist API",
    version="0.1.0",
    description="Retell tool backend with Cliniko write-back and deterministic scheduling rules.",
)


@app.on_event("startup")
def startup() -> None:
    create_schema()


@app.exception_handler(DomainError)
async def domain_error_handler(_request: Request, exc: DomainError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "failed", "code": exc.code, "instruction": exc.message},
    )


def _redact(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key in {"phone_e164", "full_name", "patient_full_name"}
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


@app.middleware("http")
async def audit_tool_calls(request: Request, call_next):
    if not request.url.path.startswith("/v1/tools/"):
        return await call_next(request)
    started = time.perf_counter()
    raw = await request.body()
    try:
        request_payload = _redact(json.loads(raw or b"{}"))
    except json.JSONDecodeError:
        request_payload = {}
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - started) * 1000)
    try:
        with SessionLocal() as db:
            db.add(
                ToolAuditLog(
                    call_id=request.headers.get("x-call-id"),
                    tool_name=request.url.path.rsplit("/", 1)[-1],
                    request_body=request_payload,
                    response_code=str(response.status_code),
                    duration_ms=duration_ms,
                )
            )
            db.commit()
    except Exception:
        logging.exception("Failed to write tool audit log")
    response.headers["Server-Timing"] = f"app;dur={duration_ms}"
    return response


app.include_router(router)


def run() -> None:
    import uvicorn

    uvicorn.run("clinic_voice.main:app", host="0.0.0.0", port=8000, reload=False)
