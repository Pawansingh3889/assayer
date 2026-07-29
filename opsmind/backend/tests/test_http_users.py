"""Assigning roles.

Registration always produces a participant, so this endpoint is the only way an account
becomes a creator — and a creator can publish surveys and read every participant's
answers. That makes it the most privileged route in the service, and the one where the
interesting cases are all about who is refused.
"""

from uuid import uuid4

from app.users.models import UserRole
from tests.auth_helpers import bearer


def _role(role: UserRole | str) -> dict:
    return {"role": role.value if isinstance(role, UserRole) else role}


# ------------------------------------------------------------------ who may


async def test_a_creator_promotes_a_participant(client, creator, participant):
    response = await client.patch(
        f"/api/v1/users/{participant.id}/role",
        json=_role(UserRole.creator),
        headers=bearer(creator),
    )

    assert response.status_code == 200
    assert response.json()["role"] == "creator"

    # The promotion is real, not just reported: the participant-only route now refuses
    # them and the creator-only route does not.
    assert (await client.get("/api/v1/templates", headers=bearer(participant))).status_code == 200
    assert (await client.get("/api/v1/runs", headers=bearer(participant))).status_code == 403


async def test_a_participant_cannot_promote_anyone_including_themselves(
    client, participant, other_participant
):
    """The obvious attack. If this were merely authenticated rather than creator-only,
    any account could grant itself every survey's responses."""
    for target in (other_participant, participant):
        response = await client.patch(
            f"/api/v1/users/{target.id}/role",
            json=_role(UserRole.creator),
            headers=bearer(participant),
        )
        assert response.status_code == 403

    assert participant.role is UserRole.participant


async def test_an_anonymous_caller_cannot_set_a_role(client, participant):
    response = await client.patch(
        f"/api/v1/users/{participant.id}/role", json=_role(UserRole.creator)
    )

    assert response.status_code == 401


# ------------------------------------------------------------------ demotion


async def test_a_creator_can_be_demoted_while_another_remains(
    client, session, creator, other_creator
):
    response = await client.patch(
        f"/api/v1/users/{other_creator.id}/role",
        json=_role(UserRole.participant),
        headers=bearer(creator),
    )

    assert response.status_code == 200
    assert response.json()["role"] == "participant"


async def test_the_last_creator_cannot_be_demoted(client, creator, participant):
    """Unrecoverable if allowed. Granting the creator role requires already holding it,
    so removing the only one leaves no account in the system able to grant it back —
    recovery would mean direct database access, which a customer install will not have.
    """
    response = await client.patch(
        f"/api/v1/users/{creator.id}/role",
        json=_role(UserRole.participant),
        headers=bearer(creator),
    )

    assert response.status_code == 409
    assert "only creator account" in response.json()["error"]["message"]
    # Still a creator, so still able to work.
    assert (await client.get("/api/v1/templates", headers=bearer(creator))).status_code == 200


async def test_demoting_the_last_creator_is_refused_even_from_another_creators_token(
    client, session, creator, other_creator
):
    """The guard counts creators; it is not a rule about demoting yourself. Demote one
    of two and the survivor is then the last, whoever asked."""
    await client.patch(
        f"/api/v1/users/{other_creator.id}/role",
        json=_role(UserRole.participant),
        headers=bearer(creator),
    )

    response = await client.patch(
        f"/api/v1/users/{creator.id}/role",
        json=_role(UserRole.participant),
        headers=bearer(creator),
    )

    assert response.status_code == 409


async def test_a_creator_can_step_down_once_a_replacement_exists(client, creator, participant):
    """The handover this is meant to allow: promote, then step down."""
    await client.patch(
        f"/api/v1/users/{participant.id}/role",
        json=_role(UserRole.creator),
        headers=bearer(creator),
    )

    response = await client.patch(
        f"/api/v1/users/{creator.id}/role",
        json=_role(UserRole.participant),
        headers=bearer(creator),
    )

    assert response.status_code == 200
    assert (await client.get("/api/v1/templates", headers=bearer(creator))).status_code == 403


# ------------------------------------------------------------------ edges


async def test_setting_the_role_an_account_already_has_is_a_no_op(client, creator, participant):
    """Idempotent rather than a conflict — the caller asked for a state the account is
    already in. It must not trip the last-creator guard either."""
    response = await client.patch(
        f"/api/v1/users/{creator.id}/role", json=_role(UserRole.creator), headers=bearer(creator)
    )

    assert response.status_code == 200
    assert response.json()["role"] == "creator"


async def test_an_unknown_user_is_a_404(client, creator):
    response = await client.patch(
        f"/api/v1/users/{uuid4()}/role", json=_role(UserRole.creator), headers=bearer(creator)
    )

    assert response.status_code == 404


async def test_a_role_that_does_not_exist_is_refused(client, creator, participant):
    response = await client.patch(
        f"/api/v1/users/{participant.id}/role", json=_role("administrator"), headers=bearer(creator)
    )

    assert response.status_code == 422


async def test_the_response_carries_no_password_hash(client, creator, participant):
    response = await client.patch(
        f"/api/v1/users/{participant.id}/role",
        json=_role(UserRole.creator),
        headers=bearer(creator),
    )

    assert set(response.json()) == {"id", "email", "display_name", "role"}
