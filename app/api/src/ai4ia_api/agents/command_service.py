"""Execution of ``/command`` directives parsed from chat input.

The chat router calls :func:`execute_command` when :func:`parse_input` finds a
slash command. The service owns all persistence + session mutation for the
command and returns the assistant reply message, which the router then streams
or returns. Keeping it here (rather than in the router) makes command behavior
unit-testable against the in-memory repository.

Commands that depend on later phases (``/summarize``) return a friendly "not
available yet" message instead of failing. ``/forget`` is wired to the memory
service; when memory is disabled it reports that nothing is stored.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..auth.base import AuthenticatedUser
from ..catalog import ModelCatalog
from ..memory.service import MemoryServiceProtocol
from ..sessions.models import Message, MessageRole, MessageStatus, Session
from ..sessions.repository import SessionRepository
from .agent_catalog import AgentCatalog
from .commands import CommandKind, ParsedInput

HELP_TEXT = (
    "Available commands:\n"
    "/help — show this message\n"
    "/clear — clear this conversation's history\n"
    "/system <prompt> — set the system prompt (no args shows the current one)\n"
    "/model <model-id> — switch the model for this conversation\n"
    "/agents — list the agents you can mention\n"
    "/summarize — summarize the conversation (coming soon)\n"
    "/forget [session|me] — erase stored memories for this chat (default) or "
    "all of yours\n"
    "Mention @agent at the start of a turn to route it to that agent "
    "(e.g. @coder review this function). Use /agents to see who's available."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def execute_command(
    *,
    parsed: ParsedInput,
    session: Session,
    user: AuthenticatedUser,
    repo: SessionRepository,
    catalog: ModelCatalog,
    agents: AgentCatalog,
    memory: MemoryServiceProtocol | None = None,
) -> Message:
    """Run the parsed command, persist its effects, and return the reply message."""
    command = parsed.command
    assert command is not None, "execute_command requires a parsed command"
    user_id = user.internal_user_id

    # /clear wipes history (including the command itself), so it skips echoing
    # the user's command message; everything else records it for context.
    if command.kind is CommandKind.clear:
        await repo.clear_messages(user_id, session.id)
        reply = "Conversation cleared."
    else:
        await repo.add_message(
            user_id,
            Message(
                sessionId=session.id,
                userId=user_id,
                role=MessageRole.user,
                content=parsed.raw,
                status=MessageStatus.complete,
                fromCommand=True,
            ),
        )
        if command.kind is CommandKind.forget:
            reply = await _forget_reply(memory, user_id, session.id, command.args)
        else:
            reply = await _reply_for(
                command.kind, command.name, command.args, session, catalog, agents
            )

    # Persist any session mutation (systemPrompt/model) BEFORE recording the
    # success reply, so a failed update can't leave a misleading transcript.
    session.updatedAt = _now()
    await repo.update_session(session)

    assistant = Message(
        sessionId=session.id,
        userId=user_id,
        role=MessageRole.assistant,
        content=reply,
        status=MessageStatus.complete,
        fromCommand=True,
    )
    await repo.add_message(user_id, assistant)
    return assistant


async def _reply_for(
    kind: CommandKind,
    name: str,
    args: str,
    session: Session,
    catalog: ModelCatalog,
    agents: AgentCatalog,
) -> str:
    if kind is CommandKind.help:
        return HELP_TEXT

    if kind is CommandKind.agents:
        return _agents_text(agents)

    if kind is CommandKind.system:
        if not args:
            current = session.systemPrompt
            return f"Current system prompt:\n{current}" if current else "No system prompt set."
        session.systemPrompt = args
        return "System prompt updated."

    if kind is CommandKind.model:
        if not args:
            return "Usage: /model <model-id>"
        if catalog.get(args) is None:
            return f"Unknown model: {args}. Pick one from the model menu."
        session.model = args
        return f"Model switched to {args}."

    if kind is CommandKind.summarize:
        return f"/{name} isn't available yet — long-chat summarize is coming soon."

    # Unknown command.
    return f"Unknown command: /{name}. Type /help to see what's available."


async def _forget_reply(
    memory: MemoryServiceProtocol | None,
    user_id: str,
    session_id: str,
    args: str,
) -> str:
    """Erase stored memories. ``/forget`` (or ``session``) clears this chat;
    ``/forget me`` (or ``all``) clears everything for the user."""
    scope = (args or "").strip().lower()
    if scope in ("", "session", "this"):
        if memory is None or not memory.enabled:
            return "There are no stored memories for this conversation."
        count = await memory.forget_session(user_id, session_id)
        return f"Forgot {count} stored {_plural(count)} from this conversation."
    if scope in ("me", "all", "everything"):
        if memory is None or not memory.enabled:
            return "There are no stored memories to forget."
        count = await memory.forget_user(user_id)
        return f"Forgot all {count} stored {_plural(count)}."
    return "Usage: /forget [session|me]"


def _plural(count: int) -> str:
    return "memory" if count == 1 else "memories"


def _agents_text(agents: AgentCatalog) -> str:
    """Render the list of mentionable agents for the /agents command."""
    available = agents.public_list()
    if not available:
        return "No agents are available yet."
    lines = ["Available agents (mention one at the start of a turn):"]
    lines.extend(f"@{a.name} — {a.displayName}: {a.description}" for a in available)
    return "\n".join(lines)
