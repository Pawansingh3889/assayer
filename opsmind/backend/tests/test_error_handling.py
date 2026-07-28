"""An exhausted or failing LLM reads as a calm 503, never a raw provider error.

Exercises the registered exception handler directly — no database or network — so it
stays fast and asserts exactly what a caller receives.
"""

import json

from starlette.requests import Request

from app.llm.base import LLMError, NoToolCallError
from app.main import app


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/v1/runs", "headers": []})


async def test_llm_failure_becomes_a_calm_503_without_raw_detail():
    handler = app.exception_handlers[LLMError]
    raw = 'Backup LLM rejected the request (429): {"error":{"code":429,"message":"quota exceeded"}}'

    response = await handler(_request(), LLMError(raw))
    body = json.loads(response.body)

    assert response.status_code == 503
    assert body["error"]["code"] == "llm_unavailable"
    assert "unavailable" in body["error"]["message"].lower()
    # the raw upstream detail never reaches the participant
    assert "429" not in body["error"]["message"]
    assert "quota" not in body["error"]["message"].lower()


def test_llm_error_subclasses_resolve_to_the_same_handler():
    # Starlette matches on the exception's MRO, so a NoToolCallError (a subclass of
    # LLMError) lands on the LLMError handler too, not the generic AppError one.
    assert issubclass(NoToolCallError, LLMError)
    assert LLMError in app.exception_handlers


async def test_a_database_that_has_gone_away_reads_as_a_calm_503():
    """The failure this exists for: when Postgres stops, asyncpg's socket error reaches
    the boundary as a plain OSError. SQLAlchemy does not wrap a failure to *connect*, so
    an OperationalError handler would miss it and every endpoint answered with a bare,
    unexplained 500."""
    handler = app.exception_handlers[OSError]

    response = await handler(_request(), ConnectionRefusedError(111, "Connection refused"))
    body = json.loads(response.body)

    assert response.status_code == 503
    assert body["error"]["code"] == "database_unavailable"
    assert "database" in body["error"]["message"]
    # The socket detail belongs in the log, not in a participant's browser.
    assert "111" not in body["error"]["message"]


async def test_health_reports_the_database_it_depends_on(session):
    """Health used to answer 'ok' whenever the process was up, so an outage looked like
    an application bug: green health, every real endpoint failing."""
    from app.main import health

    response = await health(session)
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body == {"status": "ok", "database": "ok"}


async def test_health_is_degraded_when_the_database_cannot_be_reached():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.main import health

    dead = create_async_engine("postgresql+asyncpg://opsmind:opsmind@localhost:5999/opsmind")
    async with AsyncSession(dead) as unreachable:
        response = await health(unreachable)
    await dead.dispose()
    body = json.loads(response.body)

    assert response.status_code == 503
    assert body == {"status": "degraded", "database": "unreachable"}
