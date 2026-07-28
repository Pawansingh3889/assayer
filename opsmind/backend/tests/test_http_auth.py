"""The auth boundary, over HTTP.

`app/auth/dependencies.py` had no test that went through a request. Its three
dependencies are the only thing standing between an anonymous caller and every
route, and each of their failure paths is an HTTP status the service layer never
sees — so none of it was covered.

Dev-auth is deliberately thin: the caller asserts an identity with `X-User-Id` and
nothing verifies it. These tests pin the *shape* of that boundary — who is refused,
with which code — so replacing the header with a real identity provider is a change
to one dependency that these tests keep honest.
"""

from uuid import uuid4

CREATOR_ONLY = "/api/v1/templates"
PARTICIPANT_ONLY = "/api/v1/runs"


async def test_a_request_with_no_identity_is_refused(client):
    response = await client.get(CREATOR_ONLY)

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Missing X-User-Id header."}
    }


async def test_an_identity_that_matches_no_user_is_refused(client):
    """A well-formed id for a user that does not exist is still nobody."""
    response = await client.get(CREATOR_ONLY, headers={"X-User-Id": str(uuid4())})

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Unknown user id."


async def test_an_identity_that_is_not_a_uuid_is_rejected_before_any_lookup(client):
    """FastAPI parses the header into a UUID, so a malformed one never reaches the
    repository. 422 rather than 401: the request itself is malformed."""
    response = await client.get(CREATOR_ONLY, headers={"X-User-Id": "not-a-uuid"})

    assert response.status_code == 422


async def test_a_participant_cannot_reach_a_creator_route(client, participant):
    response = await client.get(CREATOR_ONLY, headers={"X-User-Id": str(participant.id)})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_a_creator_cannot_take_a_survey(client, creator):
    """The mirror image, and the one that is easy to get wrong: `require_creator` and
    `require_participant` are separate dependencies, so a role check missing from one
    of them would not show up in the other's tests."""
    response = await client.get(PARTICIPANT_ONLY, headers={"X-User-Id": str(creator.id)})

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Only participants can take surveys."


async def test_a_creator_reaches_their_own_routes(client, creator):
    response = await client.get(CREATOR_ONLY, headers={"X-User-Id": str(creator.id)})

    assert response.status_code == 200
    assert response.json() == []


async def test_a_participant_reaches_their_own_routes(client, participant):
    response = await client.get(PARTICIPANT_ONLY, headers={"X-User-Id": str(participant.id)})

    assert response.status_code == 200
    assert response.json() == []


async def test_the_published_list_is_open_to_any_signed_in_user(client, participant, creator):
    """`/published` takes `get_current_user`, not `require_creator` — a published survey
    is exactly what a participant is meant to be able to see. Still not anonymous."""
    for user in (participant, creator):
        response = await client.get(
            "/api/v1/templates/published", headers={"X-User-Id": str(user.id)}
        )
        assert response.status_code == 200

    assert (await client.get("/api/v1/templates/published")).status_code == 401


async def test_the_user_list_is_deliberately_open(client, creator, participant):
    """`/api/v1/users` carries no auth dependency at all, because it backs the dev-auth
    user picker — you cannot pick who to be if you must already be someone.

    That is a real hole and it is on purpose, so it is pinned here rather than left to
    be discovered: whoever replaces dev-auth has to delete this test on the way past.
    """
    response = await client.get("/api/v1/users")

    assert response.status_code == 200
    assert {u["id"] for u in response.json()} == {str(creator.id), str(participant.id)}
