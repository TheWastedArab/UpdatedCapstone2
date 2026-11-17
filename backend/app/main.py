"""FastAPI application: REST interface between the VS Code extension and the triage engine."""
import asyncio
import logging
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.openapi.utils import get_openapi

from .ai.engine import engine
from .config import get_settings
from .openapi30 import downgrade_to_openapi_30
from .routers import admin, auth, bugs, notifications, projects
from .services import coldstart

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bugtracker")

API_PREFIX = "/api/v1"
RETRAIN_SWEEP_SECONDS = 6 * 3600


async def _retrain_sweep() -> None:
    """Enforces the 30-day retraining rule for projects with no recent verification events (FR-29)."""
    while True:
        await asyncio.sleep(RETRAIN_SWEEP_SECONDS)
        try:
            await run_in_threadpool(coldstart.retrain_due_projects)
        except Exception:
            log.exception("Retraining sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.environment == "production" and settings.secret_key.startswith("dev-only"):
        raise RuntimeError("BT_SECRET_KEY must be set in production")
    # Never run more handler threads than there are pooled connections, with a few connections
    # spare for background work (profile enrichment, training, re-indexing).
    threads = max(4, min(settings.worker_threads, settings.db_pool_size + settings.db_max_overflow - 5))
    anyio.to_thread.current_default_thread_limiter().total_tokens = threads
    # Models are loaded once here, never per request (NFR-02).
    await run_in_threadpool(engine.load)
    sweep = asyncio.create_task(_retrain_sweep())
    yield
    sweep.cancel()


app = FastAPI(
    title="AI-Powered Bug Tracking System API",
    version="1.0.0",
    description=(
        "REST interface of the AI-powered bug tracking system: bug lifecycle, real-time duplicate detection, "
        "automatic severity classification and developer assignment recommendations. "
        "All endpoints except registration, verification and login require a bearer token."
    ),
    lifespan=lifespan,
    docs_url=f"{API_PREFIX}/docs",
    redoc_url=None,
    openapi_url=f"{API_PREFIX}/openapi.json",
)

for module in (auth, projects, bugs, notifications, admin):
    app.include_router(module.router, prefix=API_PREFIX)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/health", tags=["Service"], summary="Liveness probe")
def health():
    return {"status": "ok", "ai": engine.status()["enabled"]}


def custom_openapi():
    if app.openapi_schema is None:
        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        app.openapi_schema = downgrade_to_openapi_30(schema)  # DC-05 specifies OpenAPI 3.0
    return app.openapi_schema


app.openapi = custom_openapi
