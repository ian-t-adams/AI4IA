"""Resolve one effective prompt, agent, and tool set for a conversation."""
from __future__ import annotations

from dataclasses import dataclass

from ..agents.agent_catalog import AgentSpec
from ..sessions.models import Session
from ..policy.context import current_binding
from ..policy.models import PolicyDecision, PolicyError
from ..publishing.models import PublicationError, PublicationExecutionMode


@dataclass(frozen=True)
class EffectiveConversationPolicy:
    agent: AgentSpec | None
    instructions: str | None
    instruction_source: str
    inherited_tools: tuple[str, ...]
    added_tools: tuple[str, ...]
    removed_tools: tuple[str, ...]
    effective_tools: tuple[str, ...]
    voice_tools: tuple[str, ...]


async def resolve_conversation_policy(
    state,
    user_id: str,
    session: Session,
    *,
    explicit_agent: str | None = None,
    mode: PublicationExecutionMode = "chat",
) -> EffectiveConversationPolicy:
    """Compose durable session settings with an optional one-turn agent override.

    The server resolves every name against the caller's composed catalog and only
    admits conversation additions from AgentService's explicit attachable allowlist.
    Execution-time ToolRegistry/MCP authorization still runs independently.
    """
    selected = (explicit_agent or session.agentName or "").strip()
    agent = None
    if selected:
        candidate = await state.agent_service.resolve_for(user_id, selected, state.agents, mode=mode)
        if session.agentVersion is not None and not explicit_agent:
            binding = current_binding()
            if binding is None or binding.owner_id != user_id:
                raise PolicyError(PolicyDecision("unavailable", "reauthentication_required"))
            resolved = await state.publications.resolve_for_execution(
                await binding.resolve(), session.agentVersion, mode=mode,
            )
            candidate = state.publications.agent_projection(resolved, selected)
        if candidate is not None and candidate.enabled:
            if candidate.sourceVersion is not None and not explicit_agent and session.agentVersion is None:
                raise PublicationError("publication_selection_unpinned")
            agent = candidate

    inherited = tuple(dict.fromkeys(agent.tools if agent is not None else ()))
    added = tuple(
        dict.fromkeys(
            name
            for name in session.toolOverrides.added
            if name not in inherited
        )
    )
    removed_set = set(session.toolOverrides.removed)
    settings = getattr(state, "settings", None)
    media_enabled = {
        "generate_image": getattr(settings, "image_generation_enabled", False),
        "generate_video": getattr(settings, "video_generation_enabled", False),
    }
    effective = tuple(
        name for name in (*inherited, *added)
        if name not in removed_set and media_enabled.get(name, True)
    )
    voice_tools = tuple(
        name
        for name in effective
        if state.tool_executor.get(name) is not None
        and state.tool_registry.get(name) is not None
    )

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
        voice_tools=voice_tools,
    )
