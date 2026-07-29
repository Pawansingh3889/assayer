"""Registering and signing in.

The boundary tests in test_http_auth.py assume a token already exists. This is where
one comes from.
"""

import pytest

from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import LocalJWT
from app.config import get_settings
from app.users.models import User, UserRole

PASSWORD = "correct-horse-battery-staple"


async def _register(client, email="new@opsmind.dev", **overrides):
    body = {"email": email, "display_name": "New Person", "password": PASSWORD}
    body.update(overrides)
    return await client.post("/api/v1/auth/register", json=body)


async def _with_password(session, email, password, role=UserRole.participant) -> User:
    user = User(
        email=email,
        display_name="Existing Person",
        role=role,
        password_hash=hash_password(password),
    )
    session.add(user)
    await session.flush()
    return user


# ------------------------------------------------------------------ register


async def test_registering_returns_a_token_that_works(client):
    response = await _register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == get_settings().jwt_ttl_minutes * 60

    # Signing up is usable in one round trip — no separate login step.
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == "new@opsmind.dev"


async def test_self_signup_can_only_ever_create_a_participant(client, session):
    """The role is not taken from the request. A creator can publish surveys and read
    every participant's answers, so it is granted deliberately — never claimed at a
    signup form. Accepting one here would make every role check theatre."""
    response = await _register(client, role="creator")

    assert response.status_code == 201
    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )
    assert me.json()["role"] == "participant"


async def test_registering_an_existing_email_is_a_conflict(client, session):
    await _with_password(session, "taken@opsmind.dev", PASSWORD)

    response = await _register(client, email="taken@opsmind.dev")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_email_case_does_not_create_a_second_account(client, session):
    """The column is unique but case-sensitive, so without folding, Ava@ and ava@ are
    two accounts — and the second person to sign up silently gets a different one."""
    await _register(client, email="Ava@Opsmind.dev")

    response = await _register(client, email="ava@opsmind.dev")

    assert response.status_code == 409


@pytest.mark.parametrize(
    "overrides",
    [
        {"password": "short"},
        {"email": "not-an-email"},
        {"display_name": "   "},
    ],
)
async def test_a_malformed_registration_is_refused(client, overrides):
    assert (await _register(client, **overrides)).status_code == 422


# --------------------------------------------------------------------- login


async def test_logging_in_returns_a_usable_token(client, session):
    user = await _with_password(session, "rosa@opsmind.dev", PASSWORD)

    response = await client.post(
        "/api/v1/auth/login", json={"email": "rosa@opsmind.dev", "password": PASSWORD}
    )

    assert response.status_code == 200
    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )
    assert me.json()["id"] == str(user.id)


async def test_logging_in_is_case_insensitive_on_the_address(client, session):
    await _with_password(session, "rosa@opsmind.dev", PASSWORD)

    response = await client.post(
        "/api/v1/auth/login", json={"email": "ROSA@Opsmind.dev", "password": PASSWORD}
    )

    assert response.status_code == 200


async def test_a_wrong_password_and_an_unknown_account_are_indistinguishable(client, session):
    """Different answers here turn login into a way to test whether an address has an
    account, which for a survey tool is a disclosure in itself."""
    await _with_password(session, "rosa@opsmind.dev", PASSWORD)

    wrong = await client.post(
        "/api/v1/auth/login", json={"email": "rosa@opsmind.dev", "password": "not-the-password"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@opsmind.dev", "password": PASSWORD}
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_an_account_with_no_password_cannot_log_in(client, session):
    """Rows predating this column, and anything an external provider would own. The
    check still runs against a dummy hash so the absence is not detectable by timing."""
    session.add(User(email="legacy@opsmind.dev", display_name="Legacy", role=UserRole.participant))
    await session.flush()

    response = await client.post(
        "/api/v1/auth/login", json={"email": "legacy@opsmind.dev", "password": PASSWORD}
    )

    assert response.status_code == 401


# ----------------------------------------------------------------------- me


async def test_me_requires_a_token(client):
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_me_does_not_return_the_password_hash(client, session):
    """`AuthenticatedUser` lists its fields explicitly, so a hash cannot leak by
    somebody later switching the response model to the ORM object."""
    user = await _with_password(session, "rosa@opsmind.dev", PASSWORD)
    token, _ = LocalJWT(get_settings()).issue(user.id)

    response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert "password" not in response.text
    assert set(response.json()) == {"id", "email", "display_name", "role"}


# ------------------------------------------------------------------ hashing


def test_a_stored_hash_is_not_the_password():
    stored = hash_password(PASSWORD)

    assert PASSWORD not in stored
    assert stored.startswith("$argon2id$")


def test_the_same_password_hashes_differently_each_time():
    """Per-hash salt. Identical hashes would tell anyone reading the table which
    accounts share a password."""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_verification_accepts_the_right_password_and_nothing_else():
    stored = hash_password(PASSWORD)

    assert verify_password(PASSWORD, stored) is True
    assert verify_password(PASSWORD + "x", stored) is False
    assert verify_password(PASSWORD, None) is False
    assert verify_password(PASSWORD, "not-a-hash") is False
