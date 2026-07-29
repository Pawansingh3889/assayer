"""Results and export, over HTTP.

The export route is the one place in the API that answers with a file rather than
JSON, and none of what makes that work — the media type, the `Content-Disposition`
filename, the literal `export` path segment — was reachable from a service-level test.
"""

from uuid import uuid4

from app.llm.base import ToolTurn
from tests.auth_helpers import bearer
from tests.fakes import move_on, record


def _as(user):
    return bearer(user)


async def _completed_run(client, participant, published, fake_llm):
    """Answer both questions so the run reaches `completed` and has rows to export.

    One FakeLLM serves both requests: the patched `get_llm` returns the same instance
    each time, so its scripted turns carry across. q0 permits probing (record, then
    move_on); q1 is a rating that does not, so recording alone completes the run.
    """
    fake_llm(record("Line lead"), move_on("And how was onboarding?"), record(5))

    started = await client.post(
        "/api/v1/runs", json={"template_id": str(published.id)}, headers=_as(participant)
    )
    run_id = started.json()["id"]

    await client.post(
        f"/api/v1/runs/{run_id}/messages",
        json={"content": "line lead"},
        headers=_as(participant),
    )
    final = await client.post(
        f"/api/v1/runs/{run_id}/messages", json={"content": "5"}, headers=_as(participant)
    )
    assert final.json()["status"] == "completed", final.text
    return run_id


async def test_a_creator_lists_the_runs_on_their_survey(
    client, creator, participant, published, fake_llm
):
    run_id = await _completed_run(client, participant, published, fake_llm)

    response = await client.get(f"/api/v1/templates/{published.id}/runs", headers=_as(creator))

    assert response.status_code == 200
    listed = response.json()
    assert [r["id"] for r in listed] == [run_id]
    assert listed[0]["participant_name"] == "Test Participant"
    assert listed[0]["status"] == "completed"


async def test_a_creator_reads_one_run_in_full(client, creator, participant, published, fake_llm):
    run_id = await _completed_run(client, participant, published, fake_llm)

    response = await client.get(
        f"/api/v1/templates/{published.id}/runs/{run_id}", headers=_as(creator)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert [a["value"] for a in body["answers"]] == [{"text": "Line lead"}, {"rating": 5}]
    assert body["messages"]  # the transcript comes back too


async def test_another_creator_cannot_read_the_results(
    client, other_creator, participant, published, fake_llm
):
    await _completed_run(client, participant, published, fake_llm)

    response = await client.get(
        f"/api/v1/templates/{published.id}/runs", headers=_as(other_creator)
    )

    assert response.status_code == 404


async def test_export_defaults_to_csv_with_a_download_filename(
    client, creator, participant, published, fake_llm
):
    """`main.py` adds `expose_headers=["Content-Disposition"]` to CORS purely so the
    browser can read this filename. Nothing tested that the header is set at all.
    """
    await _completed_run(client, participant, published, fake_llm)

    response = await client.get(
        f"/api/v1/templates/{published.id}/runs/export", headers=_as(creator)
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="onboarding-check-in-responses.csv"'
    )
    # The leading BOM is deliberate — without it Excel opens the file in the wrong
    # encoding, which is the entire reason the CSV branch exists. Assert it rather than
    # strip it, or a future "tidy-up" removes it and nothing notices until a customer
    # opens a mangled export.
    assert response.text.startswith("﻿")
    lines = response.text.lstrip("﻿").strip().splitlines()
    assert lines[0] == "run_id,participant,run_status,version,question,kind,answer,answered_at"
    assert len(lines) == 3  # header + one row per answer
    assert "Line lead" in response.text


async def test_export_can_return_json_instead(client, creator, participant, published, fake_llm):
    await _completed_run(client, participant, published, fake_llm)

    response = await client.get(
        f"/api/v1/templates/{published.id}/runs/export?format=json", headers=_as(creator)
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="onboarding-check-in-responses.json"'
    )
    rows = response.json()
    assert [r["answer"] for r in rows] == ["Line lead", "5"]
    assert {r["participant"] for r in rows} == {"Test Participant"}


async def test_an_unsupported_export_format_is_refused(client, creator, published):
    """`format` is a Literal["csv", "json"], so anything else fails at the boundary
    rather than silently falling through to CSV."""
    response = await client.get(
        f"/api/v1/templates/{published.id}/runs/export?format=pdf", headers=_as(creator)
    )

    assert response.status_code == 422


async def test_the_export_path_is_not_parsed_as_a_run_id(client, creator, published):
    """`/runs/export` is declared before `/runs/{run_id}`. Reverse the declaration order
    and `export` is parsed as a UUID: the download route disappears behind a 422, with
    nothing else to indicate why.
    """
    response = await client.get(
        f"/api/v1/templates/{published.id}/runs/export", headers=_as(creator)
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"


async def test_a_run_id_from_another_template_is_a_404(
    client, creator, participant, published, draft, fake_llm
):
    run_id = await _completed_run(client, participant, published, fake_llm)

    response = await client.get(f"/api/v1/templates/{draft.id}/runs/{run_id}", headers=_as(creator))

    assert response.status_code == 404


async def test_a_missing_run_is_a_404(client, creator, published):
    response = await client.get(
        f"/api/v1/templates/{published.id}/runs/{uuid4()}", headers=_as(creator)
    )

    assert response.status_code == 404


async def test_summarising_a_run_returns_the_structured_summary(
    client, creator, participant, published, fake_llm
):
    run_id = await _completed_run(client, participant, published, fake_llm)
    fake_llm(
        ToolTurn(
            text="",
            tool_name="summarise_run",
            tool_input={
                "headline": "A line lead who found onboarding solid.",
                "key_facts": ["Works as a line lead", "Rated onboarding 5 of 5"],
                "notable_quotes": [{"question": "What's your role?", "quote": "Line lead"}],
            },
        )
    )

    response = await client.post(
        f"/api/v1/templates/{published.id}/runs/{run_id}/summary", headers=_as(creator)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["headline"] == "A line lead who found onboarding solid."
    assert body["key_facts"] == ["Works as a line lead", "Rated onboarding 5 of 5"]
    assert body["notable_quotes"][0]["quote"] == "Line lead"


async def test_a_participant_cannot_reach_the_results_routes(client, participant, published):
    for path in (
        f"/api/v1/templates/{published.id}/runs",
        f"/api/v1/templates/{published.id}/runs/export",
    ):
        response = await client.get(path, headers=_as(participant))
        assert response.status_code == 403, path
