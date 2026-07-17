"""Execution of ``/command`` directives parsed from chat input.

The chat router calls :func:`execute_command` when :func:`parse_input` finds a
slash command. The service owns all persistence + session mutation for the
command and returns the assistant reply message, which the router then streams
or returns. Keeping it here (rather than in the router) makes command behavior
unit-testable against the in-memory repository.

``/summarize`` uses the rolling-summary service when a gateway-backed summarizer
is available. ``/forget`` is wired to the memory service; when memory is disabled
it reports that nothing is stored.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..auth.base import AuthenticatedUser
from ..catalog import ModelCatalog
from ..memory.service import MemoryServiceProtocol
from ..sessions.models import Message, MessageRole, MessageStatus, Session
from ..sessions.repository import SessionRepository
from .agent_catalog import AgentCatalog
from .commands import CommandKind, ParsedInput
from .summarization import ManualSummaryStatus, SummarizationService
from .tool_exec import (
    ToolContext,
    ToolExecutionError,
    ToolExecutor,
    ToolValidationError,
)
from .tools import ToolRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..gateway.client import ModelGatewayClient

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Available commands:\n"
    "/help — show this message\n"
    "/clear — clear this conversation's history\n"
    "/system <prompt> — set the system prompt (no args shows the current one)\n"
    "/model <model-id> — switch the model for this conversation\n"
    "/agents — list the agents you can mention\n"
    "/summarize — condense this conversation into a running summary\n"
    "/forget [session|me] — erase stored memories for this chat (default) or "
    "all of yours\n"
    "/research <query> — search the live web and cite sources when Web IQ is enabled\n"
    "/<tool> [args] — run a tool directly (e.g. /calculator (2+3)*4, "
    "/generate_image a red bicycle). Type / in the composer to see the tools.\n"
    "Mention @agent at the start of a turn to route it to that agent "
    "(e.g. @coder review this function). Use /agents to see who's available."
)


# Direct tools run locally via the executor (with an empty ToolContext) the
# instant the user types ``/<tool> args`` — no model call, no entitlement spend.
# Each entry maps the slash argument string to the tool's JSON arguments. Only
# safe, deterministic, no-scope builtins belong here; service-backed capability
# tools (generate_image/…) are routed through a model turn by the chat router.
_DIRECT_TOOL_ARGS: dict[str, Callable[[str], dict[str, Any]]] = {
    "calculator": lambda args: {"expression": args},
    "get_current_time": lambda args: {},
}
DIRECT_SLASH_TOOLS: frozenset[str] = frozenset(_DIRECT_TOOL_ARGS)


async def execute_command(
    *,
    parsed: ParsedInput,
    session: Session,
    user: AuthenticatedUser,
    repo: SessionRepository,
    catalog: ModelCatalog,
    agents: AgentCatalog,
    memory: MemoryServiceProtocol | None = None,
    summarizer: SummarizationService | None = None,
    gateway: "ModelGatewayClient | None" = None,
) -> Message:
    """Run the parsed command, persist its effects, and return the reply message."""
    command = parsed.command
    assert command is not None, "execute_command requires a parsed command"
    user_id = user.internal_user_id
    before = {
        "model": session.model,
        "systemPrompt": session.systemPrompt,
    }
    expected_summary_version: int | None = None
    persist_reply = True

    # /clear wipes history (including the command itself), so it skips echoing
    # the user's command message; everything else records it for context.
    if command.kind is CommandKind.clear:
        await repo.clear_messages(user_id, session.id)
        reply = "Conversation cleared."
        # Clear summary state as one versioned mutation. Any summarizer that
        # started before this increment will discard its stale result.
        cleared = await repo.invalidate_summary(user_id, session.id)
        session.summary = cleared.summary
        session.summarizedThroughMessageId = cleared.summarizedThroughMessageId
        session.summaryVersion = cleared.summaryVersion
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
        elif command.kind is CommandKind.summarize:
            reply, expected_summary_version, persist_reply = await _summarize_reply(
                summarizer, gateway, repo, catalog, user_id, session
            )
        else:
            reply = await _reply_for(
                command.kind, command.name, command.args, session, catalog, agents
            )

    # Persist any session mutation (systemPrompt/model) BEFORE recording the
    # success reply, so a failed update can't leave a misleading transcript.
    changes = {
        field_name: getattr(session, field_name)
        for field_name, previous in before.items()
        if getattr(session, field_name) != previous
    }
    if changes:
        await repo.patch_session(user_id, session.id, changes)
    else:
        await repo.touch_session(user_id, session.id)

    assistant = Message(
        sessionId=session.id,
        userId=user_id,
        role=MessageRole.assistant,
        content=reply,
        status=MessageStatus.complete,
        fromCommand=True,
        summaryVersion=expected_summary_version,
    )
    if not persist_reply:
        return assistant
    if expected_summary_version is not None:
        persisted = await repo.add_message_if_summary_version(
            user_id,
            assistant,
            expected_version=expected_summary_version,
        )
        if not persisted:
            assistant.content = "Summary was superseded by a newer conversation state."
            assistant.summaryVersion = None
            return assistant
    else:
        await repo.add_message(user_id, assistant)
    return assistant


async def execute_tool_command(
    *,
    parsed: ParsedInput,
    session: Session,
    user: AuthenticatedUser,
    repo: SessionRepository,
    registry: ToolRegistry,
    executor: ToolExecutor,
    correlation_id: str | None = None,
) -> Message:
    """Run a *direct* tool named by a slash command and persist the user echo +
    the result reply. These tools (see :data:`DIRECT_SLASH_TOOLS`) are
    deterministic builtins with no scopes/egress, so they execute locally with
    an empty :class:`ToolContext` — no model call and no entitlement spend."""
    command = parsed.command
    assert command is not None, "execute_tool_command requires a parsed command"
    user_id = user.internal_user_id

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

    reply = await _run_direct_tool(
        command.name, command.args, registry, executor, correlation_id
    )

    await repo.touch_session(user_id, session.id)

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


async def _run_direct_tool(
    name: str,
    args: str,
    registry: ToolRegistry,
    executor: ToolExecutor,
    correlation_id: str | None,
) -> str:
    builder = _DIRECT_TOOL_ARGS.get(name)
    if builder is None:  # pragma: no cover - guarded by the caller's routing
        return f"Unknown tool: /{name}. Type /help to see what's available."
    if name == "calculator" and not args.strip():
        return "Usage: /calculator <expression> — e.g. /calculator (2 + 3) * 4"

    # Defense in depth: confirm the registry still permits this tool before
    # running it (these builtins request no scopes/hosts/approval).
    decision = registry.authorize(
        name, granted_scopes=frozenset(), target_hosts=frozenset(), approved=False
    )
    if not decision.allowed:
        return f"/{name} isn't available right now."

    ctx = ToolContext(correlation_id=correlation_id)
    try:
        result = await executor.execute(name, builder(args), ctx)
    except (ToolValidationError, ToolExecutionError) as exc:
        return f"/{name}: {exc}"
    return _format_tool_result(name, result)


def _format_tool_result(name: str, result: Any) -> str:
    """Render a direct tool's result as a short, friendly chat reply."""
    if isinstance(result, dict):
        if name == "calculator" and "result" in result:
            expr = str(result.get("expression", "")).strip()
            value = result["result"]
            return f"`{expr}` = **{value}**" if expr else f"= **{value}**"
        if name == "get_current_time" and "utc" in result:
            return f"Current time (UTC): **{result['utc']}**"
    return f"```json\n{json.dumps(result, indent=2, default=str)}\n```"


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
        selected_agent = agents.get(session.agentName) if session.agentName else None
        if selected_agent is not None and selected_agent.enabled:
            if args:
                return (
                    f"Instructions are owned by the selected agent "
                    f"'{selected_agent.displayName}'. Edit that agent in "
                    "Agents & workflows or select the generic assistant first."
                )
            return (
                f"Effective instructions source: agent ({selected_agent.displayName}).\n"
                f"{selected_agent.systemPrompt}"
            )
        if not args:
            current = session.systemPrompt
            return (
                f"Effective instructions source: session.\n{current}"
                if current
                else "Effective instructions source: provider default."
            )
        session.systemPrompt = args
        return "Conversation instructions updated."

    if kind is CommandKind.model:
        if not args:
            return "Usage: /model <model-id>"
        if catalog.get(args) is None:
            return f"Unknown model: {args}. Pick one from the model menu."
        session.model = args
        return f"Model switched to {args}."

    # Unknown command.
    return f"Unknown command: /{name}. Type /help to see what's available."


async def _summarize_reply(
    summarizer: SummarizationService | None,
    gateway: "ModelGatewayClient | None",
    repo: SessionRepository,
    catalog: ModelCatalog,
    user_id: str,
    session: Session,
) -> tuple[str, int | None, bool]:
    """Manual ``/summarize``: condense the conversation into a running summary,
    persist it on the session (mutated in place; the caller's update_session
    commits it), and show the digest. Fail-soft: any model/store error degrades
    to a friendly message and never raises out of the command path."""
    if summarizer is None or gateway is None:
        return "Summarizing long chats isn't available in this environment yet.", None, True
    if not session.model:
        return (
            "Choose a model for this conversation first (use /model <model-id> "
            "or the model menu), then run /summarize."
        ), None, True
    deployment = catalog.resolve_deployment(session.model)
    if deployment is None:
        return f"Can't summarize: '{session.model}' is not an available model.", None, True
    entry = catalog.get(session.model)
    api = entry.api if entry is not None else "chat"
    prior = await repo.list_messages(user_id, session.id)
    try:
        result = await summarizer.summarize_now(
            gateway=gateway,
            repo=repo,
            session=session,
            user_id=user_id,
            deployment=deployment.deploymentName,
            prior=prior,
            api=api,
        )
    except Exception:  # noqa: BLE001 - a command must never crash the turn
        logger.warning("manual /summarize failed", exc_info=True)
        return (
            "Sorry — I couldn't summarize the conversation just now. "
            "Please try again in a moment."
        ), None, True
    if result.status is ManualSummaryStatus.superseded:
        return "Summary was superseded by a newer conversation state.", None, False
    if result.status is ManualSummaryStatus.insufficient:
        return "There's not enough conversation here to summarize yet.", None, True
    assert result.summary is not None and result.committed_version is not None
    return (
        f"Here's a running summary of the conversation so far:\n\n{result.summary}",
        result.committed_version,
        True,
    )


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
