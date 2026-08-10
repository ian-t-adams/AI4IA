"""User-defined agents: a per-user, durable counterpart to the curated
:class:`~ai4ia_api.agents.agent_catalog.AgentSpec` catalog.

A *user agent* is a persona a single user authors and saves (name, display name,
description, system prompt, an optional preferred model, and an optional allowlist
of **user-attachable** tools). It is stored per-user in Cosmos and merged with the
curated catalog at request time so it shows up in that user's ``@``-mention menu
and is routable exactly like a curated agent — but only for its owner.

This module defines the durable record (:class:`UserAgent`), the client-supplied
request payloads (which deliberately exclude server-owned fields like ``userId``,
``id``, and timestamps), and the typed errors the service raises (translated to
HTTP status codes by the router).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .agent_catalog import AgentSpec

# A user agent name must be a strict subset of the @mention grammar
# (``commands._MENTION_RE``) that is also a valid Cosmos item id: it must start
# with a lowercase letter and END in an alphanumeric or underscore, so it can
# never end in ``.``/``-`` (which the mention regex's trailing ``\b`` would strip,
# making the agent unmentionable). Interior ``.`` and ``-`` are allowed.
NAME_RE = re.compile(r"^[a-z](?:[a-z0-9_.-]{0,30}[a-z0-9_])?$")

MAX_AGENTS_PER_USER = 50
MAX_NAME_LEN = 32
MAX_DISPLAY_NAME_LEN = 80
MAX_DESCRIPTION_LEN = 280
MAX_SYSTEM_PROMPT_LEN = 8000
MAX_TOOLS = 8
MAX_LINKS = 5


class UserAgentError(Exception):
    """Base class for user-agent service errors."""


class AgentValidationError(UserAgentError):
    """A field failed validation (-> HTTP 422)."""


class AgentConflictError(UserAgentError):
    """Name is reserved, already exists, or the per-user cap is reached (-> 409)."""


class AgentNotFoundError(UserAgentError):
    """No user agent with that name for this user (-> 404)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserAgent(BaseModel):
    """Durable, server-side record of a user-authored agent persona.

    ``id`` equals ``name`` and is unique within the user's ``/userId`` partition,
    so reads/deletes are single-partition point operations.
    """

    id: str
    userId: str
    name: str
    displayName: str
    description: str = ""
    systemPrompt: str
    defaultModel: str | None = None
    tools: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    enabled: bool = True
    createdAt: datetime = Field(default_factory=_now)
    updatedAt: datetime = Field(default_factory=_now)

    def to_spec(self) -> AgentSpec:
        """Project to the curated-catalog shape so the resolution/routing path
        treats user and curated agents identically (owner/timestamps dropped)."""
        return AgentSpec(
            name=self.name,
            displayName=self.displayName,
            description=self.description,
            systemPrompt=self.systemPrompt,
            defaultModel=self.defaultModel,
            tools=list(self.tools),
            links=list(self.links),
            enabled=self.enabled,
        )


class UserAgentCreate(BaseModel):
    """Client payload for creating a user agent. Server-owned fields (``id``,
    ``userId``, timestamps) are intentionally absent and set server-side."""

    name: str
    displayName: str | None = None
    description: str = Field(default="", max_length=MAX_DESCRIPTION_LEN)
    systemPrompt: str = Field(min_length=1, max_length=MAX_SYSTEM_PROMPT_LEN)
    defaultModel: str | None = None
    tools: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    enabled: bool = True


class UserAgentUpdate(BaseModel):
    """Client payload for replacing a user agent (the name comes from the path)."""

    displayName: str | None = None
    description: str = Field(default="", max_length=MAX_DESCRIPTION_LEN)
    systemPrompt: str = Field(min_length=1, max_length=MAX_SYSTEM_PROMPT_LEN)
    defaultModel: str | None = None
    tools: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    enabled: bool = True
