"""Unit tests for the slash-command execution service."""
from __future__ import annotations

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
        parsed=parsed, session=session, user=user, repo=repo, catalog=load_catalog()
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
    assert msg.content == "System prompt updated."
    stored = await repo.get_session(user.internal_user_id, session.id)
    assert stored.systemPrompt == "You are a helpful pirate."


async def test_system_no_args_shows_current_then_none():
    repo, user, session = await _setup()
    session.systemPrompt = "Existing prompt"
    msg = await _run(repo, user, session, "/system")
    assert "Existing prompt" in msg.content

    session.systemPrompt = None
    msg = await _run(repo, user, session, "/system")
    assert msg.content == "No system prompt set."


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


async def test_summarize_and_forget_report_not_available():
    repo, user, session = await _setup()
    summarize = await _run(repo, user, session, "/summarize")
    assert "isn't available yet" in summarize.content
    forget = await _run(repo, user, session, "/forget")
    assert "isn't available yet" in forget.content


async def test_unknown_command_points_to_help():
    repo, user, session = await _setup()
    msg = await _run(repo, user, session, "/frobnicate")
    assert "Unknown command" in msg.content
    assert "/help" in msg.content


async def test_command_reply_marked_complete():
    repo, user, session = await _setup()
    msg = await _run(repo, user, session, "/help")
    assert msg.status is MessageStatus.complete
