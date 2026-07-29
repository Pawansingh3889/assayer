"""The auth boundary, over HTTP.

These tests previously described dev-auth, where the caller asserted an identity with
`X-User-Id` and nothing verified it. That header is gone; the shape of the boundary is
not. Who is refused, and with which code, is deliberately unchanged — which is the whole
claim `auth/dependencies.py` made when it said swapping in a real provider would touch
one dependency and no routes.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt

from app.config import get_settings
from tests.auth_helpers import bearer

CREATOR_ONLY = "/api/v1/templates"
PARTICIPANT_ONLY = "/api/v1/runs"


def _token(claims: dict, secret: str | None = None) -> dict[str, str]:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid4()),
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        **claims,
    }
    encoded = jwt.encode(payload, secret or settings.jwt_secret, algorithm="HS256")
    return {"Authorization": f"Bearer {encoded}"}


# ------------------------------------------------------------- no credentials


async def test_a_request_with_no_token_is_refused(client):
    response = await client.get(CREATOR_ONLY)

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Missing bearer token."}
    }


async def test_the_old_header_no_longer_authenticates_anyone(client, creator):
    """The point of the whole change. `X-User-Id` was trusted outright, so anyone could
    be anyone; sending it now achieves exactly nothing."""
    response = await client.get(CREATOR_ONLY, headers={"X-User-Id": str(creator.id)})

    assert response.status_code == 401


async def test_a_non_bearer_scheme_is_refused(client, creator):
    response = await client.get(CREATOR_ONLY, headers={"Authorization": f"Basic {creator.id}"})

    assert response.status_code == 401


# ------------------------------------------------------------- bad tokens


async def test_a_token_signed_with_another_key_is_refused(client, creator):
    """The one that matters most. If the signature were not checked, anyone could mint
    a token naming any user id and the service would believe it."""
    response = await client.get(
        CREATOR_ONLY,
        headers=_token(
            {"sub": str(creator.id)}, secret="an-attackers-own-key-of-respectable-length"
        ),
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Token is not valid."


async def test_an_expired_token_is_refused(client, creator):
    now = datetime.now(UTC)
    response = await client.get(
        CREATOR_ONLY,
        headers=_token(
            {
                "sub": str(creator.id),
                "iat": now - timedelta(hours=2),
                "exp": now - timedelta(hours=1),
            }
        ),
    )

    assert response.status_code == 401


async def test_a_token_from_a_different_issuer_is_refused(client, creator):
    response = await client.get(
        CREATOR_ONLY, headers=_token({"sub": str(creator.id), "iss": "somebody-else"})
    )

    assert response.status_code == 401


async def test_an_unsigned_token_is_refused(client, creator):
    """`alg: none` is the classic JWT bypass: strip the signature, declare there isn't
    one, and hope the library obliges. Verification passes an explicit algorithm
    allow-list precisely so it does not."""
    forged = jwt.encode(
        {"sub": str(creator.id), "iss": get_settings().jwt_issuer}, key="", algorithm="none"
    )

    response = await client.get(CREATOR_ONLY, headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401


async def test_garbage_in_the_header_is_refused(client):
    response = await client.get(CREATOR_ONLY, headers={"Authorization": "Bearer not.a.token"})

    assert response.status_code == 401


async def test_a_valid_token_for_a_deleted_user_is_refused(client):
    """The user is loaded every request rather than trusted from the claims, so an
    account that no longer exists cannot keep working until its token expires."""
    response = await client.get(CREATOR_ONLY, headers=_token({"sub": str(uuid4())}))

    assert response.status_code == 401


async def test_a_subject_that_is_not_a_user_id_is_refused(client):
    response = await client.get(CREATOR_ONLY, headers=_token({"sub": "ava@opsmind.dev"}))

    assert response.status_code == 401


# ------------------------------------------------------------- roles


async def test_a_participant_cannot_reach_a_creator_route(client, participant):
    response = await client.get(CREATOR_ONLY, headers=bearer(participant))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_a_creator_cannot_take_a_survey(client, creator):
    """`require_creator` and `require_participant` are separate dependencies, so a
    check missing from one would not show up in the other's tests."""
    response = await client.get(PARTICIPANT_ONLY, headers=bearer(creator))

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Only participants can take surveys."


async def test_a_creator_reaches_their_own_routes(client, creator):
    response = await client.get(CREATOR_ONLY, headers=bearer(creator))

    assert response.status_code == 200
    assert response.json() == []


async def test_a_participant_reaches_their_own_routes(client, participant):
    response = await client.get(PARTICIPANT_ONLY, headers=bearer(participant))

    assert response.status_code == 200
    assert response.json() == []


async def test_the_published_list_is_open_to_any_signed_in_user(client, participant, creator):
    """`/published` takes `get_current_user`, not `require_creator` — a published survey
    is exactly what a participant is meant to be able to see. Still not anonymous."""
    for user in (participant, creator):
        response = await client.get("/api/v1/templates/published", headers=bearer(user))
        assert response.status_code == 200

    assert (await client.get("/api/v1/templates/published")).status_code == 401


async def test_the_user_list_is_no_longer_open(client, creator, participant):
    """It was anonymous because it backed the dev-auth picker: you could not choose who
    to be if you had to already be someone. With real credentials there is nothing to
    pick, so it reverts to what it is — every account's email address."""
    assert (await client.get("/api/v1/users")).status_code == 401
    assert (await client.get("/api/v1/users", headers=bearer(participant))).status_code == 403

    response = await client.get("/api/v1/users", headers=bearer(creator))
    assert response.status_code == 200
    assert {u["id"] for u in response.json()} == {str(creator.id), str(participant.id)}
