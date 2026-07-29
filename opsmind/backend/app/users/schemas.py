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


class RoleUpdate(BaseModel):
    """The only way an account's role ever changes.

    Deliberately its own request rather than a field on a general user update: granting
    the creator role hands over every survey's responses, so it should be a thing
    somebody chose to do, not a field that came along with a display-name edit.
    """

    role: UserRole
