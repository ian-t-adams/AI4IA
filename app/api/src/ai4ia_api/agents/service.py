"""AgentService: composes the curated catalog with a user's saved agents
and owns user-agent CRUD + validation.

Resolution stays uniform: :meth:`catalog_for` returns a plain
:class:`~ai4ia_api.agents.agent_catalog.AgentCatalog` (curated + the caller's user
agents), so the chat router, ``/agents`` command, and ``@``-mention routing treat
user and curated agents identically. The curated catalog is supplied *per call*
(not held) so a test or future swap can override ``app.state.agents`` and have it
take effect immediately.

Safety: user agents can only reference an explicit allowlist of user-attachable
tools (see :func:`~ai4ia_api.agents.tool_exec.attachable_tool_names`); names that
collide with a curated agent (case-insensitively) are reserved; and a per-user
cap bounds storage. The store is best-effort for *reads* — :meth:`catalog_for`
fails open to the curated catalog if the store errors, so a Cosmos blip can never
break chat — but *writes* surface errors to the caller.
"""
from __future__ import annotations

import logging
from collections.abc import Collection

from ..catalog import ModelCatalog
from .agent_catalog import AgentCatalog
from .store import UserAgentStore
from .user_agents import (
    MAX_AGENTS_PER_USER,
    MAX_DESCRIPTION_LEN,
    MAX_DISPLAY_NAME_LEN,
    MAX_LINKS,
    MAX_SYSTEM_PROMPT_LEN,
    MAX_TOOLS,
    NAME_RE,
    AgentConflictError,
    AgentNotFoundError,
    AgentValidationError,
    UserAgent,
    UserAgentCreate,
    UserAgentUpdate,
    _now,
)

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(
        self,
        store: UserAgentStore,
        *,
        catalog: ModelCatalog,
        attachable_tools: frozenset[str],
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._attachable = attachable_tools

    @property
    def attachable_tools(self) -> frozenset[str]:
        """Server-approved tools that may be added at conversation scope."""
        return self._attachable

    async def close(self) -> None:
        await self._store.close()

    # --- Resolution -----------------------------------------------------------

    async def catalog_for(self, user_id: str, curated: AgentCatalog) -> AgentCatalog:
        """Curated catalog + this user's saved agents, as one ``AgentCatalog``.

        Curated agents win on a case-insensitive name collision (defense in depth;
        creation already reserves curated names). Fails open to ``curated`` if the
        store errors so chat never breaks on a store/Cosmos issue.
        """
        try:
            user_agents = await self._store.list(user_id)
        except Exception:  # noqa: BLE001 - reads must never break the chat path
            logger.warning("user-agent list failed; serving curated only", exc_info=True)
            return curated

        if not user_agents:
            return curated

        by_name = {a.name.lower(): a for a in curated.agents}
        merged = list(curated.agents)
        for ua in user_agents:
            if ua.name.lower() in by_name:
                continue  # curated wins
            spec = ua.to_spec()
            by_name[ua.name.lower()] = spec
            merged.append(spec)
        return AgentCatalog(agents=merged)

    async def list_for(self, user_id: str) -> list[UserAgent]:
        """The user's own full agent records (management view)."""
        return await self._store.list(user_id)

    # --- Mutations ------------------------------------------------------------

    async def create(
        self,
        user_id: str,
        req: UserAgentCreate,
        *,
        reserved_names: set[str],
        mcp_tool_names: Collection[str] | None = None,
    ) -> UserAgent:
        # Check-then-write: validate, then ensure the name is free and the per-user
        # cap isn't reached, then upsert. This is not transactional, so two
        # concurrent creates from the same user could briefly exceed the cap or
        # last-write-win on the same name. That is an accepted tradeoff: the blast
        # radius is one user's own partition (never cross-user), the windows are
        # tiny, and the data stays well-formed. Harden with a conditional
        # create_item (If-None-Match) if real concurrent double-submits appear.
        name = (req.name or "").strip().lower()
        self._validate_name(name)
        if name in {r.lower() for r in reserved_names}:
            raise AgentConflictError(f"'{name}' is a reserved agent name.")
        if await self._store.get(user_id, name) is not None:
            raise AgentConflictError(f"You already have an agent named '{name}'.")
        existing = await self._store.list(user_id)
        if len(existing) >= MAX_AGENTS_PER_USER:
            raise AgentConflictError(
                f"You have reached the maximum of {MAX_AGENTS_PER_USER} agents."
            )

        agent = self._build(
            user_id=user_id,
            name=name,
            display_name=req.displayName,
            description=req.description,
            system_prompt=req.systemPrompt,
            default_model=req.defaultModel,
            tools=req.tools,
            links=req.links,
            enabled=req.enabled,
            mcp_tool_names=mcp_tool_names,
        )
        await self._store.put(agent)
        return agent

    async def update(
        self,
        user_id: str,
        name: str,
        req: UserAgentUpdate,
        *,
        mcp_tool_names: Collection[str] | None = None,
    ) -> UserAgent:
        key = (name or "").strip().lower()
        current = await self._store.get(user_id, key)
        if current is None:
            raise AgentNotFoundError(key)
        agent = self._build(
            user_id=user_id,
            name=current.name,
            display_name=req.displayName,
            description=req.description,
            system_prompt=req.systemPrompt,
            default_model=req.defaultModel,
            tools=req.tools,
            links=req.links,
            enabled=req.enabled,
            created_at=current.createdAt,
            mcp_tool_names=mcp_tool_names,
        )
        await self._store.put(agent)
        return agent

    async def delete(self, user_id: str, name: str) -> None:
        await self._store.delete(user_id, (name or "").strip().lower())

    # --- Validation -----------------------------------------------------------

    @staticmethod
    def _validate_name(name: str) -> None:
        if not name:
            raise AgentValidationError("Agent name is required.")
        if not NAME_RE.match(name):
            raise AgentValidationError(
                "Agent name must be 1-32 chars, start with a letter, end "
                "alphanumeric, and contain only lowercase letters, digits, '_', "
                "'.', or '-' (so it can be mentioned with @name)."
            )

    def _build(
        self,
        *,
        user_id: str,
        name: str,
        display_name: str | None,
        description: str,
        system_prompt: str,
        default_model: str | None,
        tools: list[str],
        links: list[str],
        enabled: bool,
        created_at=None,
        mcp_tool_names: Collection[str] | None = None,
    ) -> UserAgent:
        display = (display_name or name).strip()
        if not display:
            display = name
        if len(display) > MAX_DISPLAY_NAME_LEN:
            raise AgentValidationError(
                f"Display name must be at most {MAX_DISPLAY_NAME_LEN} characters."
            )
        desc = (description or "").strip()
        if len(desc) > MAX_DESCRIPTION_LEN:
            raise AgentValidationError(
                f"Description must be at most {MAX_DESCRIPTION_LEN} characters."
            )
        prompt = (system_prompt or "").strip()
        if not prompt:
            raise AgentValidationError("System prompt is required.")
        if len(prompt) > MAX_SYSTEM_PROMPT_LEN:
            raise AgentValidationError(
                f"System prompt must be at most {MAX_SYSTEM_PROMPT_LEN} characters."
            )
        model = (default_model or "").strip() or None
        if model is not None and self._catalog.get(model) is None:
            raise AgentValidationError(f"Unknown model: {model}.")
        clean_tools = self._validate_tools(tools, mcp_tool_names)
        clean_links = self._validate_links(name, links)

        now = _now()
        return UserAgent(
            id=name,
            userId=user_id,
            name=name,
            displayName=display,
            description=desc,
            systemPrompt=prompt,
            defaultModel=model,
            tools=clean_tools,
            links=clean_links,
            enabled=bool(enabled),
            createdAt=created_at or now,
            updatedAt=now,
        )

    def _validate_tools(
        self, tools: list[str], mcp_tool_names: Collection[str] | None = None
    ) -> list[str]:
        if not tools:
            return []
        if len(tools) > MAX_TOOLS:
            raise AgentValidationError(f"An agent may use at most {MAX_TOOLS} tools.")
        # The caller's own discovered MCP tool names (``mcp:<server>/<tool>``) are
        # admitted in addition to the static built-in/synthetic allowlist. The
        # router only supplies these when custom tools are enabled, so when the
        # feature is off ``mcp:*`` names fall through to the rejection below exactly
        # as before.
        allowed_mcp = set(mcp_tool_names or ())
        seen: set[str] = set()
        clean: list[str] = []
        for raw in tools:
            tool = (raw or "").strip()
            if tool in seen:
                raise AgentValidationError(f"Duplicate tool: {tool}.")
            if tool not in self._attachable and tool not in allowed_mcp:
                allowed = sorted(self._attachable | allowed_mcp)
                raise AgentValidationError(
                    f"Tool '{tool}' is not available for user agents. "
                    f"Allowed: {', '.join(allowed) or '(none)'}."
                )
            seen.add(tool)
            clean.append(tool)
        return clean

    def _validate_links(self, own_name: str, links: list[str]) -> list[str]:
        """Validate the delegation targets of a user agent.

        Links are normalized to lowercase and must each look like a valid agent
        name (the ``@mention`` grammar). We dedupe, reject self-links, and cap the
        count. We deliberately do **not** check that a target exists here: the
        composed catalog is per-user and dynamic (a referenced agent may be
        created later, or be curated), so existence is resolved at *runtime* — an
        unknown or disabled target surfaces as a structured tool error during the
        turn rather than blocking a save.
        """
        if not links:
            return []
        if len(links) > MAX_LINKS:
            raise AgentValidationError(
                f"An agent may link to at most {MAX_LINKS} other agents."
            )
        self_name = (own_name or "").strip().lower()
        seen: set[str] = set()
        clean: list[str] = []
        for raw in links:
            link = (raw or "").strip().lower()
            if not link:
                raise AgentValidationError("Linked agent name must not be empty.")
            if not NAME_RE.match(link):
                raise AgentValidationError(
                    f"Linked agent name '{link}' is not a valid @mentionable name."
                )
            if link == self_name:
                raise AgentValidationError("An agent cannot link to itself.")
            if link in seen:
                raise AgentValidationError(f"Duplicate linked agent: {link}.")
            seen.add(link)
            clean.append(link)
        return clean
