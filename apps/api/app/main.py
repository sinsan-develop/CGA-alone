from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.router import api_router
from app.api.routes import am
from app.api.routes.channels import _process_queued_channel_events
from app.api.routes.bots import _process_persistent_llm_events
from app.core.config import ROOT_DIR, settings
from app.core.db_metrics import finish_db_metrics, start_db_metrics
from app.core.logging import configure_logging, get_logger
from app.db.session import SessionLocal
from app.services.edit_lock_cleanup import delete_expired_edit_locks


configure_logging()
logger = get_logger("aidot.api")
queue_worker_task: asyncio.Task | None = None
edit_lock_cleanup_task: asyncio.Task | None = None


def _queue_worker_sleep_seconds(interval: float, consecutive_failures: int) -> float:
    if consecutive_failures <= 0:
        return interval
    max_backoff = max(interval, float(settings.channel_queue_worker_error_backoff_seconds))
    return min(max_backoff, interval * (2 ** max(0, consecutive_failures - 1)))


def _process_queue_worker_batch(worker_request: SimpleNamespace, batch_size: int) -> int:
    with SessionLocal() as db:
        channel_results = _process_queued_channel_events(db, worker_request, limit=batch_size)
        db.commit()
        persistent_results = _process_persistent_llm_events(db, worker_request, limit=batch_size)
        db.commit()
    return len(channel_results) + len(persistent_results)


async def _channel_queue_worker_loop() -> None:
    interval = max(0.5, float(settings.channel_queue_worker_interval_seconds))
    batch_size = max(1, int(settings.channel_queue_worker_batch_size))
    worker_request = SimpleNamespace(client=None)
    consecutive_failures = 0
    logger.info(
        "Channel queue worker started.",
        extra={"event": "channel.queue.worker_started", "extra_data": {"interval_seconds": interval, "batch_size": batch_size}},
    )
    while True:
        try:
            channel_count = await asyncio.to_thread(
                _process_queue_worker_batch,
                worker_request,
                batch_size,
            )
            if channel_count:
                logger.info(
                    "Queue worker processed queued events.",
                    extra={
                        "event": "channel.queue.worker_processed",
                        "extra_data": {"channel_processed": channel_count},
                    },
                )
            consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as error:
            consecutive_failures += 1
            sleep_seconds = _queue_worker_sleep_seconds(interval, consecutive_failures)
            logger.exception(
                "Channel queue worker failed.",
                extra={
                    "event": "channel.queue.worker_failed",
                    "extra_data": {
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                        "consecutive_failures": consecutive_failures,
                        "next_retry_seconds": sleep_seconds,
                    },
                },
            )
        else:
            sleep_seconds = _queue_worker_sleep_seconds(interval, consecutive_failures)
        await asyncio.sleep(sleep_seconds)


async def start_channel_queue_worker() -> None:
    global queue_worker_task
    if not settings.channel_queue_worker_enabled:
        return
    if queue_worker_task is None or queue_worker_task.done():
        queue_worker_task = asyncio.create_task(_channel_queue_worker_loop())


async def stop_channel_queue_worker() -> None:
    global queue_worker_task
    if queue_worker_task is None:
        return
    queue_worker_task.cancel()
    try:
        await queue_worker_task
    except asyncio.CancelledError:
        logger.info("Channel queue worker stopped.", extra={"event": "channel.queue.worker_stopped"})
    queue_worker_task = None


def _process_edit_lock_cleanup_batch() -> int:
    with SessionLocal() as db:
        deleted_count = delete_expired_edit_locks(db)
        db.commit()
    return deleted_count


async def _edit_lock_cleanup_loop() -> None:
    interval = max(1.0, float(settings.edit_lock_cleanup_interval_seconds))
    logger.info(
        "Edit lock cleanup worker started.",
        extra={"event": "edit_lock.cleanup.worker_started", "extra_data": {"interval_seconds": interval}},
    )
    while True:
        try:
            deleted_count = await asyncio.to_thread(_process_edit_lock_cleanup_batch)
            if deleted_count:
                logger.info(
                    "Expired edit locks deleted.",
                    extra={
                        "event": "edit_lock.cleanup.completed",
                        "extra_data": {"deleted_count": deleted_count},
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception(
                "Edit lock cleanup worker failed.",
                extra={
                    "event": "edit_lock.cleanup.failed",
                    "extra_data": {"error_type": type(error).__name__, "error_message": str(error)},
                },
            )
        await asyncio.sleep(interval)


async def start_edit_lock_cleanup_worker() -> None:
    global edit_lock_cleanup_task
    if not settings.edit_lock_cleanup_enabled:
        return
    if edit_lock_cleanup_task is None or edit_lock_cleanup_task.done():
        edit_lock_cleanup_task = asyncio.create_task(_edit_lock_cleanup_loop())


async def stop_edit_lock_cleanup_worker() -> None:
    global edit_lock_cleanup_task
    if edit_lock_cleanup_task is None:
        return
    edit_lock_cleanup_task.cancel()
    try:
        await edit_lock_cleanup_task
    except asyncio.CancelledError:
        logger.info("Edit lock cleanup worker stopped.", extra={"event": "edit_lock.cleanup.worker_stopped"})
    edit_lock_cleanup_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_channel_queue_worker()
    await start_edit_lock_cleanup_worker()
    try:
        yield
    finally:
        await stop_edit_lock_cleanup_worker()
        await stop_channel_queue_worker()


app = FastAPI(
    title="CGA API",
    description="CGA Studio 제작/운영 콘솔 API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _request_log_data(request: Request, db_metrics: dict[str, int | float] | None = None) -> dict[str, object]:
    data: dict[str, object] = {
        "query_params": dict(request.query_params),
        "content_type": request.headers.get("content-type"),
        "content_length": request.headers.get("content-length"),
        "origin": request.headers.get("origin"),
        "referer": request.headers.get("referer"),
        "user_agent": request.headers.get("user-agent"),
    }
    if db_metrics is not None:
        data.update(db_metrics)
    return data


def _api_slow_request_threshold_ms() -> float:
    return max(0.0, float(settings.api_slow_request_threshold_ms))


def _should_log_slow_api_request(request: Request, elapsed_ms: float) -> bool:
    threshold_ms = _api_slow_request_threshold_ms()
    return threshold_ms > 0 and request.url.path.startswith("/api/") and elapsed_ms >= threshold_ms


static_assets_dir = Path(__file__).resolve().parent / "static" / "assets"
# 프로필 이미지 업로드 후에도 새 환경에서 공개 경로가 항상 마운트되도록 디렉터리를 준비합니다.
(ROOT_DIR / "storage" / "bot-images").mkdir(parents=True, exist_ok=True)
if static_assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=static_assets_dir), name="assets")

for category in ["temp", "bot-images"]:
    candidate_dirs = [
        ROOT_DIR / category,
        ROOT_DIR / "storage" / category,
    ]
    legacy_asset_root = settings.legacy_asset_root_path
    if legacy_asset_root is not None:
        candidate_dirs.extend(
            (
                legacy_asset_root / category,
                legacy_asset_root / "storage" / category,
            )
        )
    for static_dir in candidate_dirs:
        if static_dir.exists():
            app.mount(f"/files/{category}", StaticFiles(directory=static_dir), name=f"files-{category}")
            break


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request.state.request_id = str(uuid4())
    started_at = time.perf_counter()
    db_metrics_token = start_db_metrics()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        db_metrics = finish_db_metrics(db_metrics_token)
        logger.exception(
            "Unhandled API exception",
            extra={
                "request_id": request.state.request_id,
                "method": request.method,
                "path": request.url.path,
                "elapsed_ms": elapsed_ms,
                "client": request.client.host if request.client else None,
                "event": "api.unhandled_exception",
                "extra_data": _request_log_data(request, db_metrics),
            },
        )
        raise
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
    db_metrics = finish_db_metrics(db_metrics_token)
    logger.info(
        "API request completed",
        extra={
            "request_id": request.state.request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "client": request.client.host if request.client else None,
            "event": "api.request",
            "extra_data": _request_log_data(request, db_metrics),
        },
    )
    response.headers["X-Request-Id"] = request.state.request_id
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
    response.headers["X-Db-Time-Ms"] = f"{float(db_metrics['db_duration_ms']):.2f}"
    response.headers["X-Db-Query-Count"] = str(int(db_metrics["db_query_count"]))
    if _should_log_slow_api_request(request, elapsed_ms):
        slow_log_data = _request_log_data(request, db_metrics)
        slow_log_data["slow_threshold_ms"] = _api_slow_request_threshold_ms()
        logger.warning(
            "Slow API request detected",
            extra={
                "request_id": request.state.request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
                "client": request.client.host if request.client else None,
                "event": "api.slow_request",
                "extra_data": slow_log_data,
            },
        )
    return response


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness_check() -> JSONResponse:
    try:
        with SessionLocal() as db:
            db.execute(text("select 1"))
    except SQLAlchemyError as error:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "database": "unavailable",
                "error_type": type(error).__name__,
                "message": "Database connection is unavailable.",
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "database": "ok",
        },
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "CGA API is running."}


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


app.include_router(api_router, prefix="/api/v1")
app.include_router(am.router, prefix="/api")
