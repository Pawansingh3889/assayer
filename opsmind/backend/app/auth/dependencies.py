"""Resolving the caller from a bearer token.

This used to trust an ``X-User-Id`` header outright: whoever sent the request chose
whose it was. That was an explicit development simplification, and the note beside it
promised the swap would touch one dependency and no routes. It did — every route is
unchanged, and the role gates are the same two functions they always were.

Authorization still lives in the services, not here. These dependencies answer "who is
this, and are they broadly the right sort of user"; ownership questions — whether *this*
creator may read *that* template — stay where they already were.
"""

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import InvalidTokenError, TokenVerifier, build_verifier
from app.config import get_settings
from app.db.session import get_session
from app.errors import ForbiddenError, UnauthorizedError
from app.users.models import User, UserRole
from app.users.repository import UserRepository

# auto_error=False so a missing header arrives here as None instead of becoming
# Starlette's own 403 with a bare {"detail": ...} body. Every failure below is a 401 in
# this service's error shape, and "sent no credentials" is not a 403.
_bearer = HTTPBearer(auto_error=False)


def get_token_verifier() -> TokenVerifier:
    """A dependency so it can be overridden in tests, and swapped by configuration."""
    return build_verifier(get_settings())


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    verifier: TokenVerifier = Depends(get_token_verifier),
    session: AsyncSession = Depends(get_session),
) -> User:
    if credentials is None:
        raise UnauthorizedError("Missing bearer token.")
    try:
        subject = verifier.subject(credentials.credentials)
    except InvalidTokenError:
        # Deliberately the same message for expired, tampered, malformed and
        # wrong-issuer. Telling them apart helps whoever is probing the endpoint and
        # nobody else — the caller's remedy is to log in again in every case.
        raise UnauthorizedError("Token is not valid.") from None

    # The user is loaded on every request rather than trusted from the token's claims.
    # A token therefore cannot carry a role its holder no longer has, and deleting an
    # account takes effect at once instead of whenever the token happens to expire.
    user = await UserRepository(session).get(subject)
    if user is None:
        raise UnauthorizedError("Token is not valid.")
    return user


async def require_creator(user: User = Depends(get_current_user)) -> User:
    if user.role is not UserRole.creator:
        raise ForbiddenError("This action requires a creator account.")
    return user


async def require_participant(user: User = Depends(get_current_user)) -> User:
    if user.role is not UserRole.participant:
        raise ForbiddenError("Only participants can take surveys.")
    return user
