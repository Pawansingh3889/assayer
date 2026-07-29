"""User queries."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.users.models import User, UserRole


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        """Case-insensitively, because nobody remembers how they capitalised it.

        The column is unique but case-sensitive, so Ava@ and ava@ can both exist and
        would be different accounts. Registration folds the address before storing it;
        this folds the lookup to match.
        """
        result = await self.session.execute(select(User).where(User.email == email.strip().lower()))
        return result.scalar_one_or_none()

    async def list_all(self) -> list[User]:
        result = await self.session.execute(select(User).order_by(User.display_name))
        return list(result.scalars().all())

    async def count_with_role(self, role: UserRole) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(User).where(User.role == role)
        )
        return int(result.scalar_one())
