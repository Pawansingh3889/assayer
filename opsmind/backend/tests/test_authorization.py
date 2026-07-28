"""One creator must not reach another creator's surveys or responses.

Dev-auth is a deliberate development simplification, but these boundaries are enforced in the
services rather than in the auth layer, so they hold unchanged once a real identity
provider supplies the caller.
"""

import pytest

from app.auth.dependencies import require_creator, require_participant
from app.conduct.engine import ConductEngine
from app.errors import ForbiddenError, NotFoundError
from app.runs.service import ResultsService
from app.templates.enums import AnswerType, TemplateStatus
from app.templates.schemas import QuestionInput, TemplateCreate, TemplateUpdate
from app.templates.service import TemplateService
from tests.fakes import FakeLLM, move_on, record


def _q(text: str) -> QuestionInput:
    return QuestionInput(text=text, answer_type=AnswerType.short_text)


async def test_an_creator_only_lists_their_own_templates(session, creator, other_creator):
    svc = TemplateService(session)
    await svc.create_draft(TemplateCreate(title="Mine", questions=[_q("a")]), creator)
    await svc.create_draft(TemplateCreate(title="Theirs", questions=[_q("b")]), other_creator)

    mine = await svc.list_drafts(None, creator)
    theirs = await svc.list_drafts(None, other_creator)

    assert [t.title for t, _ in mine] == ["Mine"]
    assert [t.title for t, _ in theirs] == ["Theirs"]


@pytest.mark.parametrize("action", ["get", "update", "delete", "publish"])
async def test_another_creators_template_reads_as_absent(session, creator, other_creator, action):
    """Not found rather than forbidden, so the API can't be used to enumerate ids."""
    svc = TemplateService(session)
    mine = await svc.create_draft(TemplateCreate(title="Mine", questions=[_q("a")]), creator)

    with pytest.raises(NotFoundError):
        if action == "get":
            await svc.get_draft(mine.id, other_creator)
        elif action == "update":
            await svc.update_draft(
                mine.id, TemplateUpdate(title="Hijacked", questions=[_q("x")]), other_creator
            )
        elif action == "delete":
            await svc.delete_draft(mine.id, other_creator)
        else:
            await svc.publish(mine.id, other_creator)


async def test_an_creator_cannot_read_another_creators_responses(
    session, creator, other_creator, participant, published
):
    """Responses carry participant names and verbatim transcripts — the sensitive read."""
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    llm = FakeLLM(record("Line lead"), move_on())
    await ConductEngine(session, llm=llm).handle_message(run.id, "line lead", participant)

    results = ResultsService(session)
    assert len(await results.list_runs(published.id, creator)) == 1

    with pytest.raises(NotFoundError):
        await results.list_runs(published.id, other_creator)
    with pytest.raises(NotFoundError):
        await results.get_run(published.id, run.id, other_creator)


async def test_only_participants_can_take_surveys(creator, participant):
    """Taking a survey is participant-only; an creator is refused before a run is created."""
    assert await require_participant(user=participant) is participant
    with pytest.raises(ForbiddenError):
        await require_participant(user=creator)


async def test_only_creators_can_build(creator, participant):
    """The mirror gate: building surveys stays closed to participants."""
    assert await require_creator(user=creator) is creator
    with pytest.raises(ForbiddenError):
        await require_creator(user=participant)


async def test_published_surveys_stay_visible_to_everyone(session, creator, other_creator):
    """Published surveys aren't scoped per creator; only starting a run is role-gated
    (participant-only), and creating/results stay creator-scoped."""
    svc = TemplateService(session)
    mine = await svc.create_draft(TemplateCreate(title="Open", questions=[_q("a")]), creator)
    await svc.publish(mine.id, creator)

    listed = await svc.list_published()

    assert [t.title for t, _, _ in listed] == ["Open"]
    assert all(t.status is TemplateStatus.published for t, _, _ in listed)
