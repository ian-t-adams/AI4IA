"""Unit tests for the slash-command execution service."""
from __future__ import annotations

from ai4ia_api.agents.agent_catalog import load_agent_catalog
from ai4ia_api.agents.command_service import HELP_TEXT, execute_command
from ai4ia_api.agents.commands import parse_input
from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.catalog import load_catalog
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Message, MessageRole, MessageStatus, Session


def _user(uid: str = "u1") -> AuthenticatedUser:
    return AuthenticatedUser(
        internal_user_id=uid, subject="sub", issuer="iss", provider="dev"
    )


async def _setup() -> tuple[InMemorySessionRepository, AuthenticatedUser, Session]:
    repo = InMemorySessionRepository()
    user = _user()
    session = Session(userId=user.internal_user_id, model="gpt-5.2")
    await repo.create_session(session)
    return repo, user, session


async def _run(repo, user, session, content: str) -> Message:
    parsed = parse_input(content)
    assert parsed.is_command, content
    return await execute_command(
        parsed=parsed,
        session=session,
        user=user,
        repo=repo,
        catalog=load_catalog(),
        agents=load_agent_catalog(),
    )


async def test_help_returns_help_text_and_persists_pair():
    repo, user, session = await _setup()
    msg = await _run(repo, user, session, "/help")
    assert msg.role is MessageRole.assistant
    assert msg.content == HELP_TEXT
    msgs = await repo.list_messages(user.internal_user_id, session.id)
    assert [m.role for m in msgs] == [MessageRole.user, MessageRole.assistant]


async def test_clear_wipes_history_and_does_not_echo_command():
    repo, user, session = await _setup()
    await repo.add_message(
        user.internal_user_id,
        Message(
            sessionId=session.id,
            userId=user.internal_user_id,
            role=MessageRole.user,
            content="earlier turn",
        ),
    )
    msg = await _run(repo, user, session, "/clear")
    assert msg.content == "Conversation cleared."
    msgs = await repo.list_messages(user.internal_user_id, session.id)
    # Only the assistant confirmation remains; the /clear command is not echoed.
    assert [m.role for m in msgs] == [MessageRole.assistant]
    assert msgs[0].content == "Conversation cleared."


async def test_system_sets_prompt_and_persists():
    repo, user, session = await _setup()
    msg = await _run(repo, user, session, "/system You are a helpful pirate.")
    assert msg.content == "Conversation instructions updated."
    stored = await repo.get_session(user.internal_user_id, session.id)
    assert stored.systemPrompt == "You are a helpful pirate."


async def test_system_no_args_shows_current_then_none():
    repo, user, session = await _setup()
    session.systemPrompt = "Existing prompt"
    msg = await _run(repo, user, session, "/system")
    assert "Existing prompt" in msg.content

    session.systemPrompt = None
    msg = await _run(repo, user, session, "/system")
    assert msg.content == "Effective instructions source: provider default."


async def test_system_uses_agent_source_and_rejects_competing_edit():
    repo, user, session = await _setup()
    session.agentName = "general"
    shown = await _run(repo, user, session, "/system")
    assert "Effective instructions source: agent (General Assistant)." in shown.content
    changed = await _run(repo, user, session, "/system Ignore the agent")
    assert "Instructions are owned by the selected agent" in changed.content
    stored = await repo.get_session(user.internal_user_id, session.id)
    assert stored.systemPrompt is None


async def test_model_switch_valid():
    repo, user, session = await _setup()
    msg = await _run(repo, user, session, "/model gpt-5.1")
    assert msg.content == "Model switched to gpt-5.1."
    stored = await repo.get_session(user.internal_user_id, session.id)
    assert stored.model == "gpt-5.1"


async def test_model_unknown_does_not_change_session():
    repo, user, session = await _setup()
    msg = await _run(repo, user, session, "/model totally-made-up")
    assert "Unknown model" in msg.content
    stored = await repo.get_session(user.internal_user_id, session.id)
    assert stored.model == "gpt-5.2"


async def test_model_no_args_shows_usage():
    repo, user, session = await _setup()
    msg = await _run(repo, user, session, "/model")
    assert msg.content == "Usage: /model <model-id>"


async def test_summarize_reports_not_available_without_service():
    repo, user, session = await _setup()
    # No summarizer/gateway wired through _run, so /summarize degrades gracefully.
    summarize = await _run(repo, user, session, "/summarize")
    assert "isn't available in this environment" in summarize.content


async def test_summarize_folds_and_persists_running_summary():
    repo, user, session = await _setup()
    await repo.add_message(
        user.internal_user_id,
        Message(
            sessionId=session.id,
            userId=user.internal_user_id,
            role=MessageRole.user,
            content="a real conversation turn worth summarizing",
            status=MessageStatus.complete,
        ),
    )

    class _FakeGateway:
        async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
            return {"choices": [{"message": {"content": "RUNNING SUMMARY"}}]}

    from ai4ia_api.agents.summarization import SummarizationService

    parsed = parse_input("/summarize")
    msg = await execute_command(
        parsed=parsed,
        session=session,
        user=user,
        repo=repo,
        catalog=load_catalog(),
        agents=load_agent_catalog(),
        summarizer=SummarizationService(),
        gateway=_FakeGateway(),
    )
    assert "running summary" in msg.content.lower()
    assert "RUNNING SUMMARY" in msg.content
    stored = await repo.get_session(user.internal_user_id, session.id)
    assert stored.summary == "RUNNING SUMMARY"
    assert stored.summarizedThroughMessageId is not None


async def test_forget_without_memory_reports_nothing_stored():
    repo, user, session = await _setup()
    forget = await _run(repo, user, session, "/forget")
    assert "no stored memories" in forget.content


async def test_forget_me_and_session_with_memory_service():
    from ai4ia_api.memory.in_memory import InMemoryVectorStore
    from ai4ia_api.memory.service import MemoryService

    class _Embedder:
        async def embed(self, inputs):
            return [[1.0, 0.0] for _ in inputs]

        async def embed_one(self, text):
            return [1.0, 0.0]

    repo, user, session = await _setup()
    memory = MemoryService(
        store=InMemoryVectorStore(), embedder=_Embedder(), min_chars_to_store=4
    )
    await memory.remember(user.internal_user_id, session.id, "a durable memory line")

    parsed = parse_input("/forget me")
    reply = await execute_command(
        parsed=parsed,
        session=session,
        user=user,
        repo=repo,
        catalog=load_catalog(),
        agents=load_agent_catalog(),
        memory=memory,
    )
    assert "Forgot all 1" in reply.content


async def test_forget_rejects_unknown_scope():
    repo, user, session = await _setup()
    forget = await _run(repo, user, session, "/forget everywhere")
    assert "Usage: /forget" in forget.content


async def test_unknown_command_points_to_help():
    repo, user, session = await _setup()
    msg = await _run(repo, user, session, "/frobnicate")
    assert "Unknown command" in msg.content
    assert "/help" in msg.content


async def test_command_reply_marked_complete():
    repo, user, session = await _setup()
    msg = await _run(repo, user, session, "/help")
    assert msg.status is MessageStatus.complete
