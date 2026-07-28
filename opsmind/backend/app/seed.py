"""Idempotent dev seed: a couple of creators and a few participants.

Run with ``python -m app.seed``. Safe to run repeatedly (keyed on id), so the
stable ids below can be used as ``X-User-Id`` values while developing.
"""

import asyncio
from uuid import UUID

from app.db.session import SessionFactory
from app.users.models import User, UserRole

SEED_USERS: list[tuple[UUID, str, str, UserRole]] = [
    (
        UUID("00000000-0000-0000-0000-0000000000a1"),
        "ava@opsmind.dev",
        "Ava Whitlock",
        UserRole.creator,
    ),
    (
        UUID("00000000-0000-0000-0000-0000000000a2"),
        "arjun@opsmind.dev",
        "Arjun Rao",
        UserRole.creator,
    ),
    (
        UUID("00000000-0000-0000-0000-0000000000b1"),
        "rosa@opsmind.dev",
        "Rosa Bell",
        UserRole.participant,
    ),
    (
        UUID("00000000-0000-0000-0000-0000000000b2"),
        "ravi@opsmind.dev",
        "Ravi Nair",
        UserRole.participant,
    ),
    (
        UUID("00000000-0000-0000-0000-0000000000b3"),
        "remy@opsmind.dev",
        "Remy Fontaine",
        UserRole.participant,
    ),
]


async def seed() -> None:
    async with SessionFactory() as session:
        for uid, email, name, role in SEED_USERS:
            if await session.get(User, uid) is None:
                session.add(User(id=uid, email=email, display_name=name, role=role))
        await session.commit()
    print(f"Seeded {len(SEED_USERS)} users (idempotent).")


if __name__ == "__main__":
    asyncio.run(seed())
