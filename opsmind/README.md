# Opsmind

A standalone survey service. Creators build survey templates — by describing them in
natural language, or by hand — and publish immutable versions. Participants complete a
published survey through a conversational, LLM-driven runner that keeps the model on
rails: the engine owns which question is current, whether the run is complete, and how
many follow-ups have been spent. The model is a constrained collaborator, not the
authority.

This repository is the backend. There is no frontend yet.

## Stack

- **Python 3.12**, FastAPI (async), SQLAlchemy 2.x async + Alembic, PostgreSQL, Pydantic v2
- **LLM** — an ordered failover chain of up to three OpenAI-compatible endpoints, each
  tried until one answers. The shipped default is cloud-first with a local backstop:
  **tier 1 Cerebras → tier 2 Groq → tier 3 Ollama**, the last running as a compose
  service so it is never rate-limited and never out of credit. A disabled tier is
  skipped; when every tier fails the API returns a 503 rather than leaking an upstream
  error. See `LLM_TIER*_*` in `.env.example`.
- **Dev** — docker-compose (postgres + ollama + backend)

## Layout

```
backend/
  app/
    config.py       settings, env-driven, no defaults for required values
    db/             declarative base + async session
    users/          User model
    templates/      template, question and immutable version models, publishing,
                    show_when visibility, completion-time estimate
    runs/           run, answer and transcript models; results; AI run summary
    conduct/        the deterministic run engine (answer validation, run locking)
    llm/            OpenAI-compatible client, tier failover chain, tolerant
                    decoding of model JSON, versioned prompts
    auth/           dev-auth dependency
  migrations/       Alembic (async env)
  tests/            pytest against a real Postgres, LLM faked at the client boundary
docker-compose.yml
```

Layering is `routes → services → repositories → models`. Routes parse, resolve the
caller and call one service method. Services own business logic and transactions.
Repositories own every query. Nothing above the repository layer touches a session.

## Quick start

```bash
cp .env.example .env          # add LLM_TIER1_API_KEY (Cerebras) for the cloud tiers
docker compose up --build     # or: podman compose up --build
```

The backend applies migrations on start, so the schema is ready once it is up.
API on http://localhost:8000 — health at `/api/v1/health`, docs at `/docs`.

## Auth and seed users

Auth is deliberately thin for development: every request identifies its caller with an
`X-User-Id` header, resolved by a single dependency. Replacing that one dependency with
a real identity provider requires no change to any route. Requests without the header
get a 401.

`python -m app.seed` runs on start and is idempotent:

| Role | Name | `X-User-Id` |
|---|---|---|
| creator | Ava Whitlock | `00000000-0000-0000-0000-0000000000a1` |
| creator | Arjun Rao | `00000000-0000-0000-0000-0000000000a2` |
| participant | Rosa Bell | `00000000-0000-0000-0000-0000000000b1` |
| participant | Ravi Nair | `00000000-0000-0000-0000-0000000000b2` |
| participant | Remy Fontaine | `00000000-0000-0000-0000-0000000000b3` |

```bash
curl -s http://localhost:8000/api/v1/templates \
  -H "X-User-Id: 00000000-0000-0000-0000-0000000000a1"
```

## Tests

```bash
cd backend
DATABASE_URL=postgresql+asyncpg://opsmind:opsmind@localhost:5432/opsmind uv run pytest -q
```

The suite runs against a real Postgres, so the repository layer is exercised against the
engine it ships on, and fakes the LLM at the client wrapper so it needs no API key. If a
test ever needs a key, that is a bug in the test.

GitHub Actions runs the same gates on every pull request: `alembic upgrade head` from an
empty database, then ruff, black, mypy and pytest.

`.github/workflows/live-conduct.yml` is the opposite check — it drives real
conversations through a real model, and only runs when you press *Run workflow*, since
it costs credit. It needs an `LLM_TIER1_API_KEY` repository secret — and
`scripts/live_conversation.py`, which is not yet in this repository.

## Working outside Docker

```bash
cd backend
uv sync
uv run alembic upgrade head           # against a running postgres
uv run uvicorn app.main:app --reload
```

## Migrations

```bash
cd backend
uv run alembic revision --autogenerate -m "describe change"
uv run alembic upgrade head
```
