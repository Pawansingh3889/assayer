"""User routes. The list endpoint backs the dev-auth user picker."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.users.repository import UserRepository
from app.users.schemas import UserRead

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("", response_model=list[UserRead])
async def list_users(session: AsyncSession = Depends(get_session)) -> list[UserRead]:
    users = await UserRepository(session).list_all()
    return [UserRead.model_validate(u) for u in users]
