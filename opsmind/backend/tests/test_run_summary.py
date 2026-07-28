"""The AI summary of a completed run.

The summary is creator-facing and acted on, so the tests that matter are the gates
between the model and the stored column — not that a happy path returns a string.
"""

import pytest

from app.conduct.engine import ConductEngine
from app.errors import ConflictError, NotFoundError
from app.llm.base import LLMError, ToolTurn
from app.runs.summary import RunSummaryService
from tests.fakes import FakeLLM, move_on, record

_QUOTE = "we still count stock on paper every Friday"


def _summary(**overrides) -> ToolTurn:
    payload = {
        "headline": "Two years in and still counting stock on paper.",
        "key_facts": ["Works as a line lead", "Weekly stock counts are manual"],
        "notable_quotes": [{"question": "What's your role?", "quote": _QUOTE}],
    }
    payload.update(overrides)
    return ToolTurn(text="", tool_name="summarise_run", tool_input=payload)


async def _completed(session, participant, published):
    """A finished two-question run whose first answer contains the quotable sentence."""
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    engine = ConductEngine(session, llm=FakeLLM(record(_QUOTE), move_on()))
    run = await engine.handle_message(run.id, _QUOTE, participant)
    engine = ConductEngine(session, llm=FakeLLM(record(4), move_on()))
    return await engine.handle_message(run.id, "4", participant)


async def test_summarises_a_completed_run_and_stores_it(session, creator, participant, published):
    run = await _completed(session, participant, published)
    llm = FakeLLM(_summary())

    content = await RunSummaryService(session, llm=llm).summarise(published.id, run.id, creator)

    assert content.headline.startswith("Two years in")
    assert content.key_facts == ["Works as a line lead", "Weekly stock counts are manual"]
    assert [q.quote for q in content.notable_quotes] == [_QUOTE]
    await session.refresh(run)
    assert run.summary["headline"] == content.headline
    assert run.summary["prompt_version"] == "summarise_run_v1"
    assert run.summary["generated_at"]


async def test_a_stored_summary_is_not_regenerated(session, creator, participant, published):
    """Generation costs a model call, so the column is the cache — one call, then none."""
    run = await _completed(session, participant, published)
    llm = FakeLLM(_summary())
    service = RunSummaryService(session, llm=llm)

    first = await service.summarise(published.id, run.id, creator)
    second = await service.summarise(published.id, run.id, creator)

    assert llm.calls == 1
    assert first == second


async def test_refresh_regenerates_over_a_stored_summary(session, creator, participant, published):
    run = await _completed(session, participant, published)
    llm = FakeLLM(_summary(), _summary(headline="A second look at the same run."))
    service = RunSummaryService(session, llm=llm)

    await service.summarise(published.id, run.id, creator)
    again = await service.summarise(published.id, run.id, creator, refresh=True)

    assert llm.calls == 2
    assert again.headline == "A second look at the same run."


async def test_an_unfinished_run_is_refused(session, creator, participant, published):
    """Summarising a half-answered run would describe a response the participant is still
    giving, and would cache that description as if it were final."""
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    llm = FakeLLM(_summary())

    with pytest.raises(ConflictError, match="completed"):
        await RunSummaryService(session, llm=llm).summarise(published.id, run.id, creator)
    assert llm.calls == 0  # refused before spending a model call


async def test_an_invented_quote_is_dropped_but_the_summary_survives(
    session, creator, participant, published
):
    """A quote the participant never said is indistinguishable from a real one once it is
    rendered beside their answers. The rest of the summary is usually sound, so drop the
    quote rather than lose the whole thing."""
    run = await _completed(session, participant, published)
    llm = FakeLLM(
        _summary(
            notable_quotes=[
                {"question": "What's your role?", "quote": _QUOTE},
                {"question": "Rate your onboarding", "quote": "the training was a shambles"},
            ]
        )
    )

    content = await RunSummaryService(session, llm=llm).summarise(published.id, run.id, creator)

    assert [q.quote for q in content.notable_quotes] == [_QUOTE]


async def test_a_quote_reflowed_by_the_model_is_kept(session, creator, participant, published):
    """Models re-wrap long quotes across lines and normalise spacing. That is not
    invention, and matching on raw bytes would throw away every genuine long quote."""
    run = await _completed(session, participant, published)
    reflowed = _QUOTE.replace(" ", "\n  ").upper()
    llm = FakeLLM(_summary(notable_quotes=[{"question": "Q", "quote": reflowed}]))

    content = await RunSummaryService(session, llm=llm).summarise(published.id, run.id, creator)

    assert len(content.notable_quotes) == 1


async def test_stringified_lists_from_a_weak_model_are_decoded(
    session, creator, participant, published
):
    """Backup models emit the right list wrapped in a string; that is a serialization
    artifact, not a content problem."""
    run = await _completed(session, participant, published)
    llm = FakeLLM(_summary(key_facts='["Works as a line lead", "Counts stock by hand"]'))

    content = await RunSummaryService(session, llm=llm).summarise(published.id, run.id, creator)

    assert content.key_facts == ["Works as a line lead", "Counts stock by hand"]


async def test_an_invalid_summary_is_retried_once_then_fails_loudly(
    session, creator, participant, published
):
    run = await _completed(session, participant, published)
    recovers = FakeLLM(_summary(headline="   "), _summary())

    content = await RunSummaryService(session, llm=recovers).summarise(
        published.id, run.id, creator
    )
    assert recovers.calls == 2
    assert content.headline.startswith("Two years in")
    assert "rejected" in recovers.messages_seen[1][-1]["content"]

    stubborn = FakeLLM(_summary(headline=""))
    with pytest.raises(LLMError, match="after one retry"):
        await RunSummaryService(session, llm=stubborn).summarise(
            published.id, run.id, creator, refresh=True
        )


async def test_another_creator_cannot_summarise_someone_elses_run(
    session, creator, other_creator, participant, published
):
    run = await _completed(session, participant, published)
    llm = FakeLLM(_summary())

    with pytest.raises(NotFoundError):
        await RunSummaryService(session, llm=llm).summarise(published.id, run.id, other_creator)
    assert llm.calls == 0
