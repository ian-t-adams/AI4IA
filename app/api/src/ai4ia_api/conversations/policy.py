"""Resolve one effective prompt, agent, and tool set for a conversation."""
from __future__ import annotations

from dataclasses import dataclass

from ..agents.agent_catalog import AgentSpec
from ..sessions.models import Session


@dataclass(frozen=True)
class EffectiveConversationPolicy:
    agent: AgentSpec | None
    instructions: str | None
    instruction_source: str
    inherited_tools: tuple[str, ...]
    added_tools: tuple[str, ...]
    removed_tools: tuple[str, ...]
    effective_tools: tuple[str, ...]


async def resolve_conversation_policy(
    state,
    user_id: str,
    session: Session,
    *,
    explicit_agent: str | None = None,
) -> EffectiveConversationPolicy:
    """Compose durable session settings with an optional one-turn agent override.

    The server resolves every name against the caller's composed catalog and only
    admits conversation additions from AgentService's explicit attachable allowlist.
    Execution-time ToolRegistry/MCP authorization still runs independently.
    """
    selected = (explicit_agent or session.agentName or "").strip()
    agent = None
    if selected:
        catalog = await state.agent_service.catalog_for(user_id, state.agents)
        candidate = catalog.get(selected)
        if candidate is not None and candidate.enabled:
            agent = candidate

    inherited = tuple(dict.fromkeys(agent.tools if agent is not None else ()))
    attachable = state.agent_service.attachable_tools
    added = tuple(
        dict.fromkeys(
            name
            for name in session.toolOverrides.added
            if name in attachable and name not in inherited
        )
    )
    removed_set = set(session.toolOverrides.removed)
    effective = tuple(name for name in (*inherited, *added) if name not in removed_set)

    if agent is not None:
        instructions = agent.systemPrompt
        source = "agent"
    elif session.systemPrompt:
        instructions = session.systemPrompt
        source = "session"
    else:
        instructions = None
        source = "default"

    return EffectiveConversationPolicy(
        agent=agent,
        instructions=instructions,
        instruction_source=source,
        inherited_tools=inherited,
        added_tools=added,
        removed_tools=tuple(name for name in session.toolOverrides.removed if name in inherited),
        effective_tools=effective,
    )
