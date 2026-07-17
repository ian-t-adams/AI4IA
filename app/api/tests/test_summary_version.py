import asyncio

from ai4ia_api.agents.agent_catalog import load_agent_catalog
from ai4ia_api.agents.command_service import execute_command
from ai4ia_api.agents.commands import parse_input
from ai4ia_api.agents.summarization import SummarizationService
from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.catalog import load_catalog
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Message, MessageRole, Session


class BlockingSummaryGateway:
    def __init__(self, text: str) -> None:
        self.text = text
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, **_kwargs):
        self.started.set()
        await self.release.wait()
        return {"choices": [{"message": {"content": self.text}}]}


def _service() -> SummarizationService:
    return SummarizationService(
        enabled=True,
        recent_turns=1,
        threshold_ratio=0.1,
        fallback_threshold_chars=1,
    )


def _prior(session_id: str) -> list[Message]:
    return [
        Message(
            sessionId=session_id,
            userId="u1",
            role=MessageRole.user,
            content="first " * 20,
        ),
        Message(
            sessionId=session_id,
            userId="u1",
            role=MessageRole.assistant,
            content="second " * 20,
        ),
    ]


async def test_clear_invalidates_inflight_summary():
    repo = InMemorySessionRepository()
    created = await repo.create_session(Session(userId="u1", model="gpt-5.2"))
    prior = _prior(created.id)
    for message in prior:
        await repo.add_message("u1", message)
    stale = await repo.get_session("u1", created.id)
    gateway = BlockingSummaryGateway("stale summary")
    pending = asyncio.create_task(
        _service().apply(
            gateway=gateway,
            repo=repo,
            session=stale,
            user_id="u1",
            deployment="dep",
            prior=prior,
            system_prompt=None,
            context_window=None,
        )
    )
    await gateway.started.wait()
    await execute_command(
        parsed=parse_input("/clear"),
        session=await repo.get_session("u1", created.id),
        user=AuthenticatedUser(
            internal_user_id="u1", subject="sub", issuer="iss", provider="dev"
        ),
        repo=repo,
        catalog=load_catalog(),
        agents=load_agent_catalog(),
    )
    gateway.release.set()
    await pending

    final = await repo.get_session("u1", created.id)
    assert final.summary is None
    assert final.summarizedThroughMessageId is None
    assert final.summaryVersion == 1
    messages = await repo.list_messages("u1", created.id)
    assert [message.role for message in messages] == [MessageRole.assistant]
    assert messages[0].content == "Conversation cleared."


async def test_same_version_summary_commits_and_advances_version():
    repo = InMemorySessionRepository()
    session = await repo.create_session(Session(userId="u1", model="gpt-5.2"))
    gateway = BlockingSummaryGateway("current summary")
    gateway.release.set()
    await _service().apply(
        gateway=gateway,
        repo=repo,
        session=await repo.get_session("u1", session.id),
        user_id="u1",
        deployment="dep",
        prior=_prior(session.id),
        system_prompt=None,
        context_window=None,
    )
    final = await repo.get_session("u1", session.id)
    assert final.summary == "current summary"
    assert final.summaryVersion == 1


async def test_two_summarizers_cannot_overwrite_newer_commit():
    repo = InMemorySessionRepository()
    session = await repo.create_session(Session(userId="u1", model="gpt-5.2"))
    prior = _prior(session.id)
    older = BlockingSummaryGateway("older")
    newer = BlockingSummaryGateway("newer")
    older_task = asyncio.create_task(
        _service().apply(
            gateway=older,
            repo=repo,
            session=await repo.get_session("u1", session.id),
            user_id="u1",
            deployment="dep",
            prior=prior,
            system_prompt=None,
            context_window=None,
        )
    )
    newer_task = asyncio.create_task(
        _service().apply(
            gateway=newer,
            repo=repo,
            session=await repo.get_session("u1", session.id),
            user_id="u1",
            deployment="dep",
            prior=prior,
            system_prompt=None,
            context_window=None,
        )
    )
    await asyncio.gather(older.started.wait(), newer.started.wait())
    newer.release.set()
    await newer_task
    older.release.set()
    await older_task
    final = await repo.get_session("u1", session.id)
    assert final.summary == "newer"
    assert final.summaryVersion == 1
