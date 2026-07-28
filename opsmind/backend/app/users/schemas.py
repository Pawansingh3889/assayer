"""User response schemas."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.users.models import UserRole


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str
    role: UserRole
