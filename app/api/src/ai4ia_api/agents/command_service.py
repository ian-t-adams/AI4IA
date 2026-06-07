"""Execution of ``/command`` directives parsed from chat input.

The chat router calls :func:`execute_command` when :func:`parse_input` finds a
slash command. The service owns all persistence + session mutation for the
command and returns the assistant reply message, which the router then streams
or returns. Keeping it here (rather than in the router) makes command behavior
unit-testable against the in-memory repository.

Commands that depend on later phases (``/summarize``, ``/forget`` need memory)
return a friendly "not available yet" message instead of failing.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..auth.base import AuthenticatedUser
from ..catalog import ModelCatalog
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
    "/summarize — summarize the conversation (coming with memory)\n"
    "/forget — erase stored memories (coming with memory)\n"
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

    if kind in (CommandKind.summarize, CommandKind.forget):
        return f"/{name} isn't available yet — it arrives with the memory feature."

    # Unknown command.
    return f"Unknown command: /{name}. Type /help to see what's available."


def _agents_text(agents: AgentCatalog) -> str:
    """Render the list of mentionable agents for the /agents command."""
    available = agents.public_list()
    if not available:
        return "No agents are available yet."
    lines = ["Available agents (mention one at the start of a turn):"]
    lines.extend(f"@{a.name} — {a.displayName}: {a.description}" for a in available)
    return "\n".join(lines)
