"""Changing what an account is allowed to do.

Roles are assigned, never claimed. Registration always produces a participant, and the
only route to anything more is an existing creator granting it here — so the set of
people who can read every survey's responses only ever grows by a deliberate act.

There is no admin tier above creator. A creator is the top of the system, which is what
makes the last-creator guard below load-bearing rather than defensive.
"""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError, NotFoundError
from app.users.models import User, UserRole
from app.users.repository import UserRepository

logger = logging.getLogger("app.users")


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = UserRepository(session)

    async def set_role(self, user_id: UUID, role: UserRole, actor: User) -> User:
        """Grant or withdraw a role. The caller has already been checked as a creator."""
        user = await self.repo.get(user_id)
        if user is None:
            raise NotFoundError("User not found.")

        if user.role is role:
            # Idempotent rather than a 409: the caller asked for a state, and the
            # account is already in it. Nothing to log, because nothing changed.
            return user

        if user.role is UserRole.creator and role is not UserRole.creator:
            await self._refuse_if_last_creator(user)

        previous = user.role
        user.role = role
        await self.session.commit()
        await self.session.refresh(user)
        # A privilege change is exactly the event somebody will want to account for
        # later, and it is invisible in the data afterwards — the row just shows the
        # new role, with nothing to say who chose it or when.
        logger.info(
            "role changed: user=%s %s -> %s by=%s", user.id, previous.value, role.value, actor.id
        )
        return user

    async def _refuse_if_last_creator(self, user: User) -> None:
        """Demoting the only creator is unrecoverable, so it is refused.

        Creator is the highest role and granting it requires already holding it. Take
        the last one away — including from yourself, which is the likely way this
        happens — and no account left in the system can ever grant it back. Recovery
        means direct database access, which is precisely what a customer install will
        not have to hand.
        """
        if await self.repo.count_with_role(UserRole.creator) <= 1:
            raise ConflictError(
                "This is the only creator account. Promote another account first — "
                "demoting the last one would leave nobody able to grant the role back."
            )
