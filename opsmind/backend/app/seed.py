"""Idempotent dev seed: a couple of creators and a few participants.

Run with ``python -m app.seed``. Safe to run repeatedly (keyed on id).

These accounts have a **published, shared password**, which is only defensible because
this never runs anywhere real: docker-compose.prod.yml deliberately omits the seed step,
and the whole point of these rows is that a developer can log in without inventing five
accounts first. Override it with ``SEED_PASSWORD`` if that assumption ever stops holding.

Creators exist only here. Self-signup creates participants, so the only way to get an
account that can publish surveys and read everyone's answers is to be given one.
"""

import asyncio
import os
from uuid import UUID

from app.auth.passwords import hash_password
from app.db.session import SessionFactory
from app.users.models import User, UserRole

SEED_PASSWORD = os.environ.get("SEED_PASSWORD", "opsmind-dev-password")

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
    # Hashed once rather than per user: Argon2 is deliberately slow, and five of them
    # on every container start is five times a cost paid for no reason.
    password_hash = hash_password(SEED_PASSWORD)
    async with SessionFactory() as session:
        for uid, email, name, role in SEED_USERS:
            existing = await session.get(User, uid)
            if existing is None:
                session.add(
                    User(
                        id=uid,
                        email=email,
                        display_name=name,
                        role=role,
                        password_hash=password_hash,
                    )
                )
            elif existing.password_hash is None:
                # Seeded before passwords existed. Without this the accounts survive
                # the migration but can never log in, which looks like a broken build.
                existing.password_hash = password_hash
        await session.commit()
    print(f"Seeded {len(SEED_USERS)} users (idempotent). Password: {SEED_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(seed())
