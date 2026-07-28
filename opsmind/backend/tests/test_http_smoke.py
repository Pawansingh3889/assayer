"""The harness itself: prove a request reaches the app and comes back shaped.

Everything below the routes was already tested by calling services directly. These
are the first tests that go through FastAPI — routing, dependency resolution, response
model serialisation and status codes — so if this file fails, nothing else in the
`test_http_*` files can be trusted either.
"""

from app.errors import DATABASE_UNAVAILABLE_MESSAGE


async def test_health_reports_ok_when_the_database_answers(client):
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_health_reports_degraded_when_the_database_is_gone(client, monkeypatch):
    """Readiness, not liveness.

    The endpoint used to answer "ok" whenever the process was up, so a stopped Postgres
    left health green while every real endpoint failed. This pins the branch that
    replaced it — and it is only reachable through HTTP, because the status code and
    the body are the whole behaviour.
    """
    from app.db.session import get_session
    from app.main import app

    class _DeadSession:
        async def execute(self, *_args, **_kwargs):
            raise OSError("connection refused")

    async def _dead():
        yield _DeadSession()

    monkeypatch.setitem(app.dependency_overrides, get_session, _dead)

    response = await client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "unreachable"}


async def test_an_unknown_path_is_a_404(client):
    assert (await client.get("/api/v1/nothing-here")).status_code == 404


async def test_the_database_being_unreachable_is_a_503_not_a_500(client, monkeypatch):
    """`errors.py` casts a deliberately wide net over OSError.

    SQLAlchemy does not wrap a failure to *connect*: asyncpg's socket error propagates
    as a plain OSError, so catching OperationalError would miss the case that actually
    happens when Postgres stops. Nothing exercised that handler before.
    """
    from app.db.session import get_session
    from app.main import app

    async def _refuses():
        raise OSError("connection refused")
        yield  # pragma: no cover - generator shape only

    monkeypatch.setitem(app.dependency_overrides, get_session, _refuses)

    response = await client.get("/api/v1/templates/published")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "database_unavailable"
    assert body["error"]["message"] == DATABASE_UNAVAILABLE_MESSAGE
