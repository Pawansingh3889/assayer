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

## Auth

Email and password in, a signed bearer token out. Send it on every request:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"ava@opsmind.dev","password":"opsmind-dev-password"}' | jq -r .access_token)

curl -s http://localhost:8000/api/v1/templates -H "Authorization: Bearer $TOKEN"
```

| Route | |
|---|---|
| `POST /api/v1/auth/register` | Self-signup. **Always creates a participant** — the role is not read from the request |
| `POST /api/v1/auth/login` | Email + password → token |
| `GET /api/v1/auth/me` | Who the token belongs to |
| `PATCH /api/v1/users/{id}/role` | **Creator-only.** The only way an account's role ever changes |

Roles are assigned, never claimed. Registration always produces a participant, and a
creator can publish surveys and read every participant's answers — so that role is
granted by an existing creator, or by seeding:

```bash
curl -s -X PATCH http://localhost:8000/api/v1/users/$USER_ID/role \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"role":"creator"}'
```

Demoting the **last** creator is refused. Granting the role requires already holding it,
so removing the only one would leave no account able to grant it back, and recovery
would mean direct database access. Promote a replacement first, then step down — that
handover is the case the rule exists to allow.

Passwords are Argon2id, minimum twelve characters and no composition rules — those push
people towards `Passw0rd!` and measurably lower entropy. Tokens are HS256, valid for
`JWT_TTL_MINUTES` (12 hours by default), and carry only a subject; the account and its
role are read from the database on every request, so a token cannot hold a role its
owner has lost and deleting an account takes effect immediately.

**`JWT_SECRET` must be set before deploying.** The default is a published development
key, and anyone holding it can sign in as anyone — so `APP_ENV=prod` refuses to start
while it is still in place.

`AUTH_PROVIDER=oidc` is the seam for single sign-on, not a working mode: verification
sits behind a port (`app/auth/tokens.py`) so an external issuer is a swap rather than a
rewrite, but `OIDCTokenVerifier` is a documented stub. `buildplan.md` defers SSO until a
client asks for it.

### Seed users

`python -m app.seed` runs on start **in development only** and is idempotent. It is
deliberately absent from `docker-compose.prod.yml`: it would leave working accounts,
with a published password, on a customer's install.

All five share the password `opsmind-dev-password` — override with `SEED_PASSWORD`.

| Role | Name | Email |
|---|---|---|
| creator | Ava Whitlock | `ava@opsmind.dev` |
| creator | Arjun Rao | `arjun@opsmind.dev` |
| participant | Rosa Bell | `rosa@opsmind.dev` |
| participant | Ravi Nair | `ravi@opsmind.dev` |
| participant | Remy Fontaine | `remy@opsmind.dev` |

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
