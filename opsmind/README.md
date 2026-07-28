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

That file is for development only. It bind-mounts the source over the image, runs
`uvicorn --reload`, seeds demo users on every boot, and publishes Postgres to the host —
all correct locally and all wrong on an install. Deployments use a separate file:

```bash
POSTGRES_PASSWORD=… FRONTEND_ORIGIN=https://… \
  docker compose -f docker-compose.prod.yml up -d
```

It pins every image by digest, keeps the database off the host's interfaces, applies
migrations but does **not** seed, and sets `APP_ENV=prod` — which makes an unusable LLM
configuration a startup failure rather than a warning. It still builds the backend image
from source; a release should pull a pre-built, signed one, which is what `PIPELINE.md`
§3 is for.

## Auth and seed users

Auth is deliberately thin for development: every request identifies its caller with an
`X-User-Id` header, resolved by a single dependency. Replacing that one dependency with
a real identity provider requires no change to any route. Requests without the header
get a 401.

`python -m app.seed` runs on start **in development only** and is idempotent. It is
deliberately absent from `docker-compose.prod.yml`: dev-auth trusts any user id it is
handed, so seeding a customer install would create working accounts nobody asked for.

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

`DATABASE_URL` decides which server the suite uses; the test database is that URL with a
`_test` suffix, created on first run. It used to be hardcoded to `localhost:5432`, which
meant the command above had no effect and, on a machine already running something else
on 5432, the suite would connect to *that* server.

The suite runs against a real Postgres, so the repository layer is exercised against the
engine it ships on, and fakes the LLM at the client wrapper so it needs no API key. If a
test ever needs a key, that is a bug in the test. Roughly a fifth of it now goes through
FastAPI rather than calling services directly — see `tests/test_http_*.py` — so the
routers, auth dependencies, error handlers and status codes are covered too.

GitHub Actions runs the same gates on every pull request: `alembic upgrade head` from an
empty database, then ruff, black, mypy and pytest. **That workflow lives at the
repository root**, `../.github/workflows/ci.yml`, not under `opsmind/`. GitHub reads
workflows only from the root, so while it sat here it never ran at all.

`.github/workflows/live-conduct.yml` is the opposite check — it drives real
conversations through a real model, and only runs when you press *Run workflow*, since
it costs credit. **It cannot run today**: it invokes `scripts/live_conversation.py`,
which does not exist anywhere in this tree, so a dispatch fails on that step. It is left
here rather than moved to the root, because relocating a broken job only moves the
breakage. Restoring the script is what makes it real; it also needs an
`LLM_TIER1_API_KEY` repository secret.

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
