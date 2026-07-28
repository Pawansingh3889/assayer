"""FastAPI application entrypoint.

Routes stay thin and delegate to services; domain routers are mounted here.
"""

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.conduct.router import router as runs_router
from app.config import get_settings
from app.db.session import get_session
from app.errors import register_error_handlers
from app.llm.factory import get_llm
from app.runs.router import router as results_router
from app.templates.router import router as templates_router
from app.users.router import router as users_router


def _configure_logging() -> None:
    """Give the app's own loggers somewhere to write.

    Without this they had nowhere to go. The app runs as ``uvicorn app.main:app``, and
    uvicorn's default config attaches handlers only to the ``uvicorn*`` loggers — the root
    gets none — so every ``app.*`` record fell through to ``logging.lastResort``, which
    drops anything below WARNING. The per-call token-usage lines we treat as
    non-negotiable are logged at INFO, so across a full day of live runs not one was ever
    written, and the warnings that did survive arrived as a bare message with no level or
    source.

    Configured on the ``app`` logger rather than the root: uvicorn owns its own
    configuration, and reconfiguring the root either fights it or double-emits every line.
    Every logger in this codebase already sits under that namespace, so one handler covers
    all of them. ``python -m app.seed`` and Alembic do not import this module and are
    unaffected; both print their own output.
    """
    app_logger = logging.getLogger("app")
    if app_logger.handlers:  # tests import this module repeatedly
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    app_logger.addHandler(handler)
    app_logger.setLevel(get_settings().log_level.upper())
    app_logger.propagate = False


_configure_logging()
logger = logging.getLogger("app.main")


def check_llm_configuration() -> None:
    """Build the LLM chain once at startup, so a misconfiguration is a boot failure.

    ``get_llm`` is called lazily, on the first turn that needs a model. That is right
    for latency — starting and reading a run should not construct a client — but it
    means "no tier configured at all" first surfaced as a ``RuntimeError`` on a
    participant's first message, mid-survey, as a 500. The deployment looked healthy
    right up until somebody used it.

    Constructing the chain makes no network call, so this stays a configuration check
    rather than a liveness probe against a provider: an unreachable Ollama must not
    stop the service booting, since the failover chain exists precisely to survive that.

    Fatal only in prod. The test suite deliberately configures no tier — it fakes the
    model at the client boundary and must pass without a key — so in dev this is a
    warning. Raising here unconditionally would turn every test red.
    """
    settings = get_settings()
    try:
        get_llm()
    except (RuntimeError, ValueError) as exc:
        if settings.app_env == "prod":
            raise
        logger.warning("no usable LLM configuration (app_env=%s): %s", settings.app_env, exc)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    check_llm_configuration()
    yield


app = FastAPI(title="Opsmind Survey Service", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().frontend_origin],
    allow_methods=["*"],
    allow_headers=["*"],
    # Without this the browser can read a download's body but not its filename.
    expose_headers=["Content-Disposition"],
)
register_error_handlers(app)
app.include_router(users_router)
app.include_router(templates_router)
app.include_router(results_router)
app.include_router(runs_router)


class HealthRead(BaseModel):
    status: str
    database: str


@app.get("/api/v1/health", response_model=HealthRead, tags=["meta"])
async def health(session: AsyncSession = Depends(get_session)) -> Response:
    """Readiness, not just liveness.

    This used to answer "ok" whenever the process was up, which is the least useful
    thing it could say: when Postgres stopped, health stayed green while every real
    endpoint failed, and the app looked broken for no visible reason. Both callers that
    poll this — the demo reset and the live-conduct workflow — are waiting to find out
    whether the API can actually serve, so answer that question.
    """
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — any failure here means "not ready"
        logger.error("health check could not reach the database: %r", exc)
        return JSONResponse(
            status_code=503,
            content=HealthRead(status="degraded", database="unreachable").model_dump(),
        )
    return JSONResponse(content=HealthRead(status="ok", database="ok").model_dump())
