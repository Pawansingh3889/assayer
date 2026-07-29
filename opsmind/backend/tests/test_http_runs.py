"""Conducting a run, over HTTP.

The engine's own decisions are covered thoroughly in test_conduct.py. What was not
covered is everything the route adds on top: the 201, the assembled `RunRead`, the
progress counters, request validation, and the ownership check expressed as a status
code rather than an exception type.
"""

from uuid import uuid4

from app.errors import LLM_UNAVAILABLE_MESSAGE
from app.llm.base import LLMError
from tests.auth_helpers import bearer
from tests.fakes import move_on, record


def _as(user):
    return bearer(user)


async def _start(client, participant, published):
    response = await client.post(
        "/api/v1/runs", json={"template_id": str(published.id)}, headers=_as(participant)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_starting_a_run_returns_the_opening_question(client, participant, published):
    body = await _start(client, participant, published)

    assert body["status"] == "in_progress"
    assert body["current_question"]["text"] == "What's your role?"
    assert body["current_question"]["answer_type"] == "short_text"
    assert (body["answered"], body["total"]) == (0, 2)
    # The engine writes the opening line itself, before any model is involved.
    assert len(body["messages"]) == 1
    assert "What's your role?" in body["messages"][0]["content"]


async def test_starting_a_run_needs_no_model(client, participant, published):
    """`ConductEngine.llm` is a lazy property precisely so starting and reading a run
    never builds a client. If that regressed, this test would fail trying to reach a
    provider — no tier is configured in the suite."""
    await _start(client, participant, published)


async def test_a_template_with_no_published_version_is_a_conflict(client, participant, draft):
    response = await client.post(
        "/api/v1/runs", json={"template_id": str(draft.id)}, headers=_as(participant)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_answering_advances_to_the_next_question(client, participant, published, fake_llm):
    run = await _start(client, participant, published)
    # q0 permits probing, so the engine loops once more for the probe decision.
    fake_llm(record("Line lead"), move_on("Thanks. How was onboarding?"))

    response = await client.post(
        f"/api/v1/runs/{run['id']}/messages",
        json={"content": "line lead"},
        headers=_as(participant),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["current_question"]["text"] == "Rate your onboarding"
    assert (body["answered"], body["total"]) == (1, 2)
    assert body["messages"][-1]["content"] == "Thanks. How was onboarding?"
    assert [a["value"] for a in body["answers"]] == [{"text": "Line lead"}]


async def test_a_blank_message_is_refused_before_it_costs_a_model_turn(
    client, participant, published
):
    """`RunMessageRequest` strips whitespace before checking min_length, so "   " is
    empty. No LLM is installed here: if the request reached the engine, the test would
    fail trying to build a client rather than returning 422."""
    run = await _start(client, participant, published)

    response = await client.post(
        f"/api/v1/runs/{run['id']}/messages",
        json={"content": "   "},
        headers=_as(participant),
    )

    assert response.status_code == 422


async def test_one_participant_cannot_read_anothers_run(
    client, participant, other_participant, published
):
    run = await _start(client, participant, published)

    response = await client.get(f"/api/v1/runs/{run['id']}", headers=_as(other_participant))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_one_participant_cannot_post_into_anothers_run(
    client, participant, other_participant, published, fake_llm
):
    """The ownership check must come before anything is written. A FakeLLM with no
    scripted turns is installed deliberately: if the engine were reached at all it
    would raise, so a 403 proves the request stopped at the boundary."""
    run = await _start(client, participant, published)
    fake_llm()

    response = await client.post(
        f"/api/v1/runs/{run['id']}/messages",
        json={"content": "let me in"},
        headers=_as(other_participant),
    )

    assert response.status_code == 403


async def test_a_missing_run_is_a_404(client, participant):
    response = await client.get(f"/api/v1/runs/{uuid4()}", headers=_as(participant))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_the_resumable_list_is_not_parsed_as_a_run_id(client, participant, published):
    """`GET /api/v1/runs` is declared before `GET /api/v1/runs/{run_id}` so the empty
    literal path wins the match. Swap the declaration order and this 200 becomes a 422
    from a failed UUID parse — a silent regression with no other symptom.
    """
    run = await _start(client, participant, published)

    response = await client.get("/api/v1/runs", headers=_as(participant))

    assert response.status_code == 200
    listed = response.json()
    assert [r["id"] for r in listed] == [run["id"]]
    assert listed[0]["title"] == "Onboarding check-in"
    assert (listed[0]["answered"], listed[0]["total"]) == (0, 2)


async def test_every_tier_failing_is_a_calm_503_that_hides_the_provider_error(
    client, participant, published, fake_llm
):
    """The failover chain re-raises the last provider error, and a participant must
    never see it. `errors.py` registers a handler on the LLMError base so subclasses
    resolve there too — nothing exercised that over HTTP before.
    """
    run = await _start(client, participant, published)
    fake_llm(LLMError("cerebras 429: quota exhausted for org acct_9f21"))

    response = await client.post(
        f"/api/v1/runs/{run['id']}/messages",
        json={"content": "line lead"},
        headers=_as(participant),
    )

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "llm_unavailable"
    assert body["error"]["message"] == LLM_UNAVAILABLE_MESSAGE
    # The bit that matters: no provider, no quota, no account id.
    assert "cerebras" not in response.text.lower()
    assert "acct_9f21" not in response.text
