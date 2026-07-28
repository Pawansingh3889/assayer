"""Test fixtures: a real Postgres test database with a fresh schema per test.

Uses the compose Postgres (a separate ``opsmind_test`` database), so repository and
service logic is exercised against the real engine, not a stand-in.

Where that Postgres lives comes from ``DATABASE_URL`` — the same variable CI sets and
the README tells you to set when running pytest. It used to be hardcoded to
``localhost:5432``, so setting ``DATABASE_URL`` for the suite did nothing and the
documented command was misleading. Worse, on a machine already running something else
on 5432, the suite would connect to *that* database and create ``opsmind_test`` inside
it. The default below is the old literal, so nothing changes for anyone who sets
nothing.
"""

import os

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.base import Base
from app.runs import models as _runs  # noqa: F401  (register tables on metadata)
from app.templates import models as _templates  # noqa: F401
from app.templates.enums import AnswerType
from app.templates.schemas import QuestionInput, TemplateCreate
from app.templates.service import TemplateService
from app.users.models import User, UserRole

DEFAULT_URL = "postgresql+asyncpg://opsmind:opsmind@localhost:5432/opsmind"

_admin = make_url(os.environ.get("DATABASE_URL") or DEFAULT_URL)
# The test database is the configured one with a ``_test`` suffix, so pointing
# DATABASE_URL at a real database can never make the suite drop its tables.
TEST_DB = f"{_admin.database}_test"

ADMIN_URL = _admin.render_as_string(hide_password=False)
TEST_URL = _admin.set(database=TEST_DB).render_as_string(hide_password=False)


@pytest_asyncio.fixture
async def engine():
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        found = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DB}
        )
        if not found:
            # CREATE DATABASE takes no bind parameters, so the identifier is quoted
            # rather than bound. TEST_DB is derived from the operator's own env.
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    await admin.dispose()

    eng = create_async_engine(TEST_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    async with AsyncSession(engine, expire_on_commit=False) as sess:
        yield sess


@pytest_asyncio.fixture
async def creator(session):
    user = User(email="creator@test.dev", display_name="Test Creator", role=UserRole.creator)
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def other_creator(session):
    """A second creator, for proving one creator cannot reach another's work."""
    user = User(email="other@test.dev", display_name="Other Creator", role=UserRole.creator)
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def participant(session):
    user = User(
        email="participant@test.dev", display_name="Test Participant", role=UserRole.participant
    )
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def published(session, creator):
    """A published two-question survey: q0 permits follow-ups, q1 is a rating that does not."""
    svc = TemplateService(session)
    template = await svc.create_draft(
        TemplateCreate(
            title="Onboarding check-in",
            questions=[
                QuestionInput(
                    text="What's your role?",
                    answer_type=AnswerType.short_text,
                    allow_follow_ups=True,
                ),
                QuestionInput(text="Rate your onboarding", answer_type=AnswerType.rating),
            ],
        ),
        creator,
    )
    await svc.publish(template.id, creator)
    return template


@pytest_asyncio.fixture
async def other_participant(session):
    """A second participant, for proving one cannot resume another's run."""
    user = User(
        email="second@test.dev", display_name="Second Participant", role=UserRole.participant
    )
    session.add(user)
    await session.flush()
    return user
