"""Access tokens, behind a port.

Two things are deliberately separate here:

``TokenVerifier`` answers one question — *which user is this request from?* — and is the
only thing the request path depends on. ``TokenIssuer`` mints tokens and exists only for
the local provider, because when somebody else's identity provider issues them there is
nothing here to mint.

That split is the whole point of the seam. ARCHITECTURE.md AD-7 argues for ports at every
adopted boundary, and identity is the one this service is most likely to have chosen for
it: a customer with SSO will not accept a second password store. Moving to OIDC should be
writing ``OIDCTokenVerifier`` and changing a setting, not touching a route.

The verifier returns a *subject*, never a ``User``. Roles and account state are read from
the database on every request, so a token cannot carry a stale role, and revoking someone
does not mean waiting for their token to expire.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import jwt

from app.config import Settings


class InvalidTokenError(Exception):
    """The token is missing, malformed, expired, or not signed by us.

    One type for every failure, and the message never says which. "Expired" versus
    "bad signature" is free information for someone probing the endpoint, and the
    caller's remedy — log in again — is identical either way.
    """


class TokenVerifier(Protocol):
    def subject(self, token: str) -> UUID:
        """The user id this token asserts, or raise InvalidTokenError."""
        ...


class TokenIssuer(Protocol):
    def issue(self, user_id: UUID) -> tuple[str, int]:
        """Return (token, seconds_until_expiry)."""
        ...


class LocalJWT:
    """Issues and verifies our own HS256 tokens.

    Symmetric signing is right here and would be wrong under OIDC. The same process
    signs and checks, so there is no second party needing a public key, and HS256
    avoids shipping key management nobody asked for. An external issuer will be
    asymmetric, which is exactly why verification sits behind the port.
    """

    def __init__(self, settings: Settings) -> None:
        self._secret = settings.jwt_secret
        self._issuer = settings.jwt_issuer
        self._ttl = timedelta(minutes=settings.jwt_ttl_minutes)

    def issue(self, user_id: UUID) -> tuple[str, int]:
        now = datetime.now(UTC)
        expires = now + self._ttl
        token = jwt.encode(
            {"sub": str(user_id), "iss": self._issuer, "iat": now, "exp": expires},
            self._secret,
            algorithm="HS256",
        )
        return token, int(self._ttl.total_seconds())

    def subject(self, token: str) -> UUID:
        try:
            # algorithms is an allow-list, not a hint. Omitting it lets a caller
            # choose "none" and hand over an unsigned token of their own devising.
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("Token is not valid.") from exc

        try:
            return UUID(str(claims["sub"]))
        except ValueError as exc:
            raise InvalidTokenError("Token subject is not a user id.") from exc


class OIDCTokenVerifier:
    """Not implemented — the shape of the work, recorded where it will be done.

    Deliberately a stub rather than a half-working implementation. Authentication that
    is *almost* right is worse than authentication that refuses to start: a verifier
    that skipped audience checking, or trusted a JWKS document it fetched over a
    redirect, would pass every test anyone thought to write and still accept tokens
    minted for a different application.

    Finishing it means, at minimum:

    - fetch and cache the issuer's JWKS (``jwt.PyJWKClient`` does this), keyed on the
      token's ``kid``, refreshing on an unknown one so key rotation is not an outage
    - verify with the issuer's asymmetric algorithm, ``iss`` and ``aud`` both checked;
      an unchecked ``aud`` accepts tokens issued for any other client of the same IdP
    - map the subject to a local user, provisioning on first sight, since the id in the
      token is the IdP's and not ours — probably a new ``users.external_subject`` column
      rather than overloading the primary key
    - decide what happens to local passwords once an IdP is authoritative

    buildplan.md defers SSO until a client asks for it. This class is where that lands.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def subject(self, token: str) -> UUID:
        raise NotImplementedError(
            "AUTH_PROVIDER=oidc is not implemented yet. See OIDCTokenVerifier in "
            "app/auth/tokens.py for what it needs. Use AUTH_PROVIDER=local."
        )


def build_verifier(settings: Settings) -> TokenVerifier:
    if settings.auth_provider == "oidc":
        return OIDCTokenVerifier(settings)
    return LocalJWT(settings)


def build_issuer(settings: Settings) -> TokenIssuer:
    """Only the local provider mints tokens; under OIDC the identity provider does.

    Raising here rather than returning something inert means /auth/login cannot quietly
    hand out tokens the verifier would never accept.
    """
    if settings.auth_provider == "oidc":
        raise NotImplementedError(
            "Tokens are issued by the identity provider when AUTH_PROVIDER=oidc; "
            "this service has no login endpoint in that mode."
        )
    return LocalJWT(settings)
