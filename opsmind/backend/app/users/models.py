"""User model and role enum."""

import enum
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserRole(str, enum.Enum):
    creator = "creator"
    participant = "participant"


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole, name="user_role"))
    # Nullable, and it has to be: an account authenticated by an external identity
    # provider never has a password here, and neither do the rows that predate this
    # column. An account with no hash cannot log in locally — see auth/passwords.py,
    # which still does the work of checking so that fact is not visible from outside.
    #
    # Wide enough for an Argon2id PHC string with room for the cost parameters to grow.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
