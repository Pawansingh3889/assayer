"""Request and response schemas for authentication."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.users.models import UserRole

# 12 rather than 8. A length floor is the only password rule here on purpose:
# composition rules ("one capital, one symbol") push people towards Passw0rd! and
# measurably reduce entropy, which is why NIST stopped recommending them.
Password = Annotated[str, StringConstraints(min_length=12, max_length=1024)]

# The upper bound is not cosmetic. Argon2 hashes whatever it is given, so an
# unbounded password field is a request-sized amount of work per login attempt.


class RegisterRequest(BaseModel):
    email: EmailStr
    display_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    password: Password


class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class TokenResponse(BaseModel):
    """OAuth-shaped, because clients and tooling already know this shape.

    No refresh token: it would need storage and revocation to be worth anything, and
    a long-lived access token that cannot be revoked is what a refresh token exists to
    avoid. Until sessions are a real requirement, logging in again is the renewal.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Seconds until the token stops being accepted")


class AuthenticatedUser(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    display_name: str
    role: UserRole
