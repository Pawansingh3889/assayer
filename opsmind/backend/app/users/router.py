"""User routes."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_creator
from app.db.session import get_session
from app.users.models import User
from app.users.repository import UserRepository
from app.users.schemas import UserRead

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
