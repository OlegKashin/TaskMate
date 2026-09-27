import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.oauth_routes import router as oauth_router
from app.api.v1.routes import router as api_router
from app.bot.telegram import router as telegram_router
from app.core.config import get_settings
from app.core.errors import AppError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    stop_event = asyncio.Event()
    polling_task = None
    if settings.telegram_mode.lower() == "polling" and settings.telegram_bot_token:
        from app.bot.polling import start_polling_background
        polling_task = asyncio.create_task(start_polling_background(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        if polling_task:
            try:
                await asyncio.wait_for(polling_task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                pass


app = FastAPI(title="TaskMate AI", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)
app.include_router(oauth_router)
app.include_router(telegram_router)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(AppError)
async def app_error(request: Request, exc: AppError):
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=exc.status_code,
        media_type="application/problem+json",
        content={
            "type": f"https://taskmate.ai/errors/{exc.code}",
            "title": exc.code.replace("_", " ").title(),
            "status": exc.status_code,
            "detail": exc.message,
            "instance": request.url.path,
            "code": exc.code,
            "request_id": request_id,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
            },
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=422,
        media_type="application/problem+json",
        content={
            "type": "https://taskmate.ai/errors/VALIDATION_ERROR",
            "title": "Validation Error",
            "status": 422,
            "detail": "Request validation failed",
            "instance": request.url.path,
            "code": "VALIDATION_ERROR",
            "details": exc.errors(),
            "request_id": request_id,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
                "details": exc.errors(),
                "request_id": request_id,
            },
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}
