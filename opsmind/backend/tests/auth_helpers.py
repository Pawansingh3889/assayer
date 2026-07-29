"""Signing tests in.

`bearer` mints a real token with the same `LocalJWT` the app verifies with, against the
same settings — so every authenticated test exercises the actual signature path. A
fixture that overrode the verifier instead would prove only that the override worked.

It replaces the one-line `_as()` helper each HTTP test file used to carry, which built
an `X-User-Id` header. That the swap is confined to this file is the point: the routes,
the assertions and the fixtures are all untouched.
"""

from app.auth.tokens import LocalJWT
from app.config import get_settings
from app.users.models import User

SEED_PASSWORD = "correct-horse-battery-staple"


def bearer(user: User) -> dict[str, str]:
    """An Authorization header carrying a valid token for this user."""
    token, _ = LocalJWT(get_settings()).issue(user.id)
    return {"Authorization": f"Bearer {token}"}
