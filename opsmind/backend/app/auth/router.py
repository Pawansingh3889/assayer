"""Authentication routes. Thin: parse, call one service method, shape the response."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_201_CREATED

from app.auth.dependencies import get_current_user
from app.auth.schemas import AuthenticatedUser, LoginRequest, RegisterRequest, TokenResponse
from app.auth.service import AuthService
from app.auth.tokens import TokenIssuer, build_issuer
from app.config import get_settings
from app.db.session import get_session
from app.users.models import User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def get_token_issuer() -> TokenIssuer:
    return build_issuer(get_settings())


@router.post("/register", response_model=TokenResponse, status_code=HTTP_201_CREATED)
async def register(
    data: RegisterRequest,
    session: AsyncSession = Depends(get_session),
    issuer: TokenIssuer = Depends(get_token_issuer),
) -> TokenResponse:
    """Create a participant account and return a token for it.

    Returning the token means signing up does not have to be immediately followed by
    signing in — the account exists and is usable in one round trip.
    """
    _, token, expires_in = await AuthService(session, issuer).register(data)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post("/login", response_model=TokenResponse)
async def login(
    data: LoginRequest,
    session: AsyncSession = Depends(get_session),
    issuer: TokenIssuer = Depends(get_token_issuer),
) -> TokenResponse:
    _, token, expires_in = await AuthService(session, issuer).login(data.email, data.password)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=AuthenticatedUser)
async def me(user: User = Depends(get_current_user)) -> AuthenticatedUser:
    """Who the bearer token belongs to.

    This is what a client needs on load to decide what to render, and it replaces the
    user picker that the old header auth required — a list of everyone's account was
    only ever a way to choose an identity you had not proved.
    """
    return AuthenticatedUser.model_validate(user)
