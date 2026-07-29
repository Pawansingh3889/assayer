"""Template routes, over HTTP.

The service layer is well covered already. These tests are about the things only a
request produces: the 201s, the 204, the serialised `TemplateRead`, request-body
validation, and — the one with a real failure mode — literal path segments winning
the match against `/{template_id}`.
"""

from uuid import uuid4

from app.llm.base import ToolTurn
from tests.auth_helpers import bearer

DRAFTED = {
    "title": "Shift handover",
    "description": "End-of-shift check",
    "questions": [
        {"text": "Anything outstanding?", "answer_type": "long_text"},
        {"text": "Line used", "answer_type": "single_select", "options": ["A", "B"]},
    ],
}


def _as(user):
    return bearer(user)


def _drafted(payload=None, note="Drafted it."):
    return ToolTurn(text=note, tool_name="draft_survey_template", tool_input=payload or DRAFTED)


async def test_creating_a_draft_returns_201_and_the_saved_template(client, creator):
    response = await client.post(
        "/api/v1/templates",
        json={"title": "Onboarding", "questions": [{"text": "Role?", "answer_type": "short_text"}]},
        headers=_as(creator),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Onboarding"
    assert body["status"] == "draft"
    assert body["created_by"] == str(creator.id)
    assert [q["text"] for q in body["questions"]] == ["Role?"]
    assert body["questions"][0]["position"] == 0


async def test_a_select_with_no_options_is_refused_by_the_request_schema(client, creator):
    """Schema-valid but nonsense: a single_select nobody can answer. The validator lives
    on `TemplateWrite`, so this is a 422 from the boundary, not a 500 from the service."""
    response = await client.post(
        "/api/v1/templates",
        json={"title": "X", "questions": [{"text": "Pick", "answer_type": "single_select"}]},
        headers=_as(creator),
    )

    assert response.status_code == 422


async def test_a_condition_pointing_forwards_is_refused(client, creator):
    """A show_when on a later question could never be true, so the survey would ship
    with a question nobody can reach."""
    response = await client.post(
        "/api/v1/templates",
        json={
            "title": "Conditional",
            "questions": [
                {
                    "text": "Second",
                    "answer_type": "short_text",
                    "show_when": {"question": 1, "op": "equals", "value": "yes"},
                },
                {"text": "First", "answer_type": "short_text"},
            ],
        },
        headers=_as(creator),
    )

    assert response.status_code == 422


async def test_a_creator_lists_only_their_own_drafts(client, creator, other_creator):
    await client.post(
        "/api/v1/templates",
        json={"title": "Mine", "questions": [{"text": "a", "answer_type": "short_text"}]},
        headers=_as(creator),
    )
    await client.post(
        "/api/v1/templates",
        json={"title": "Theirs", "questions": [{"text": "b", "answer_type": "short_text"}]},
        headers=_as(other_creator),
    )

    mine = await client.get("/api/v1/templates", headers=_as(creator))

    assert [t["title"] for t in mine.json()] == ["Mine"]


async def test_another_creators_template_is_a_404_not_a_403(client, creator, other_creator, draft):
    """404 rather than 403 on purpose: a 403 would confirm the id exists."""
    response = await client.get(f"/api/v1/templates/{draft.id}", headers=_as(other_creator))

    assert response.status_code == 404


async def test_updating_replaces_the_questions(client, creator, draft):
    response = await client.put(
        f"/api/v1/templates/{draft.id}",
        json={
            "title": "Renamed",
            "questions": [{"text": "Only question now", "answer_type": "rating"}],
        },
        headers=_as(creator),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Renamed"
    assert [q["text"] for q in body["questions"]] == ["Only question now"]


async def test_deleting_a_draft_returns_204_with_no_body(client, creator, draft):
    response = await client.delete(f"/api/v1/templates/{draft.id}", headers=_as(creator))

    assert response.status_code == 204
    assert response.content == b""

    gone = await client.get(f"/api/v1/templates/{draft.id}", headers=_as(creator))
    assert gone.status_code == 404


async def test_publishing_returns_201_and_version_one(client, creator, draft):
    response = await client.post(f"/api/v1/templates/{draft.id}/publish", headers=_as(creator))

    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 1
    assert body["template_id"] == str(draft.id)


async def test_publishing_an_empty_template_is_a_conflict(client, creator):
    created = await client.post(
        "/api/v1/templates", json={"title": "Empty", "questions": []}, headers=_as(creator)
    )
    template_id = created.json()["id"]

    response = await client.post(f"/api/v1/templates/{template_id}/publish", headers=_as(creator))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_the_published_list_is_not_parsed_as_a_template_id(client, creator, published):
    """`/published` is declared before `/{template_id}`. Swap them and this becomes a
    422 from a failed UUID parse — the route silently disappears with no other symptom.
    """
    response = await client.get("/api/v1/templates/published", headers=_as(creator))

    assert response.status_code == 200
    body = response.json()
    assert [t["title"] for t in body] == ["Onboarding check-in"]
    # Only the published list carries an estimate; a draft has none to give.
    assert body[0]["estimated_minutes"] is not None


async def test_generating_a_draft_returns_201_and_the_models_note(client, creator, fake_llm):
    fake_llm(_drafted(note="Kept it to two questions so it stays under a minute."))

    response = await client.post(
        "/api/v1/templates/generate",
        json={"prompt": "a short end-of-shift handover survey"},
        headers=_as(creator),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["note"] == "Kept it to two questions so it stays under a minute."
    assert body["template"]["title"] == "Shift handover"
    assert [q["text"] for q in body["template"]["questions"]] == [
        "Anything outstanding?",
        "Line used",
    ]


async def test_an_empty_generate_prompt_is_refused(client, creator):
    response = await client.post(
        "/api/v1/templates/generate", json={"prompt": ""}, headers=_as(creator)
    )

    assert response.status_code == 422


async def test_refining_rewrites_the_existing_draft(client, creator, draft, fake_llm):
    fake_llm(_drafted(note="Swapped the free-text question for a rating."))

    response = await client.post(
        f"/api/v1/templates/{draft.id}/refine",
        json={"instruction": "make it quicker to answer"},
        headers=_as(creator),
    )

    assert response.status_code == 200
    body = response.json()
    # Same template, replaced content — refine updates in place rather than creating.
    assert body["template"]["id"] == str(draft.id)
    assert body["template"]["title"] == "Shift handover"


async def test_refining_a_template_that_does_not_exist_is_a_404(client, creator, fake_llm):
    fake_llm(_drafted())

    response = await client.post(
        f"/api/v1/templates/{uuid4()}/refine",
        json={"instruction": "anything"},
        headers=_as(creator),
    )

    assert response.status_code == 404
