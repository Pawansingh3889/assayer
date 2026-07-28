"""Test fixtures: a real Postgres test database with a fresh schema per test.

Uses the compose Postgres (a separate ``opsmind_test`` database), so repository and
service logic is exercised against the real engine, not a stand-in.
"""

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db.base import Base
from app.runs import models as _runs  # noqa: F401  (register tables on metadata)
from app.templates import models as _templates  # noqa: F401
from app.templates.enums import AnswerType
from app.templates.schemas import QuestionInput, TemplateCreate
from app.templates.service import TemplateService
from app.users.models import User, UserRole

ADMIN_URL = "postgresql+asyncpg://opsmind:opsmind@localhost:5432/opsmind"
TEST_URL = "postgresql+asyncpg://opsmind:opsmind@localhost:5432/opsmind_test"


@pytest_asyncio.fixture
async def engine():
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        found = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = 'opsmind_test'"))
        if not found:
            await conn.execute(text("CREATE DATABASE opsmind_test"))
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
