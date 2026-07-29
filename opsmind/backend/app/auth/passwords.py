"""Password hashing.

Argon2id, at the argon2-cffi defaults. Those defaults are the library's own current
recommendation and move with it, which is the point — a cost factor pinned here would
be frozen at whatever seemed reasonable the day it was written.

Nothing outside this module knows which algorithm is in use. Hashes carry their own
parameters in the PHC string, so raising the cost later re-hashes on next login rather
than invalidating everyone's password.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()

# Hashing a throwaway password at import gives `verify_password` something real to
# check against when no user matched. Comparing against a constant string would return
# immediately and make "no such account" measurably faster than "wrong password" —
# which is a user-enumeration oracle on the login endpoint, and the reason this exists.
_DUMMY_HASH = _hasher.hash("this-is-not-anyone's-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """True if the password matches. Takes roughly the same time either way.

    ``password_hash`` is None for accounts that have no password — an OIDC-provisioned
    user, say. Those cannot log in locally, but the check still does the work so the
    absence of a password is not detectable from outside.
    """
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash predates the library's current cost parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True
