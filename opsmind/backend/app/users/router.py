"""User routes."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_creator
from app.db.session import get_session
from app.users.models import User
from app.users.repository import UserRepository
from app.users.schemas import RoleUpdate, UserRead
from app.users.service import UserService

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("", response_model=list[UserRead])
async def list_users(
    _: User = Depends(require_creator),
    session: AsyncSession = Depends(get_session),
) -> list[UserRead]:
    """Every account, for a creator.

    This was open to anyone, because it backed the dev-auth picker: you could not
    choose who to be if you had to already be someone. With real credentials there is
    nothing to pick, and the endpoint reverts to what it actually is — a list of every
    account's email address. Creator-only, rather than merely authenticated, since a
    participant has no reason to enumerate the other people answering a survey.
    """
    users = await UserRepository(session).list_all()
    return [UserRead.model_validate(u) for u in users]


@router.patch("/{user_id}/role", response_model=UserRead)
async def set_user_role(
    user_id: UUID,
    data: RoleUpdate,
    creator: User = Depends(require_creator),
    session: AsyncSession = Depends(get_session),
) -> UserRead:
    """Grant or withdraw a role. Creator-only, because creator is the role being granted.

    Registration always produces a participant; this is the only way an account becomes
    anything else. Keeping it here rather than accepting a role at signup is the whole
    point — a role a caller can ask for is not a permission, and every check that reads
    one would be decoration.
    """
    user = await UserService(session).set_role(user_id, data.role, creator)
    return UserRead.model_validate(user)
