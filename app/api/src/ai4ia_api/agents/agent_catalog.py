"""Curated, data-driven **agent catalog** (Phase 4).

An *agent* is a named persona the chat layer can route a turn to via an
``@mention``. Each agent carries a server-side ``systemPrompt`` (its persona), an
optional preferred model, and an allowlist of tool names governed by the
tool-safety registry. Concrete execution (Microsoft Agent Framework, Foundry
toolbox, MCP) plugs in on top of these declarations; this module only *describes*
agents and resolves a mention to one.

Two shapes are intentionally separate:

- :class:`AgentSpec` — the full internal record (includes ``systemPrompt`` and
  ``tools``). Server-side only.
- :class:`AgentSummary` — the public projection returned by ``GET /api/agents``
  for the frontend's ``@``-menu. It deliberately omits the system prompt and
  tool wiring so internal prompt/runtime config is never exposed to end users.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

_PACKAGED = Path(__file__).resolve().parent.parent / "data" / "agents.json"


class AgentSpec(BaseModel):
    """Full, server-side definition of an agent persona."""

    name: str
    displayName: str
    description: str
    systemPrompt: str
    defaultModel: str | None = None
    tools: list[str] = []
    enabled: bool = True

    def summary(self) -> AgentSummary:
        return AgentSummary(
            name=self.name,
            displayName=self.displayName,
            description=self.description,
            enabled=self.enabled,
        )


class AgentSummary(BaseModel):
    """Public projection of an agent (no system prompt or tool wiring)."""

    name: str
    displayName: str
    description: str
    enabled: bool = True


class AgentCatalog(BaseModel):
    agents: list[AgentSpec]

    def get(self, name: str) -> AgentSpec | None:
        """Resolve an agent by name, case-insensitively (mentions are lowercased)."""
        if not name:
            return None
        key = name.lower()
        return next((a for a in self.agents if a.name.lower() == key), None)

    def enabled_agents(self) -> list[AgentSpec]:
        return [a for a in self.agents if a.enabled]

    def public_list(self) -> list[AgentSummary]:
        """Enabled agents only, projected to the public summary shape."""
        return [a.summary() for a in self.agents if a.enabled]


def _load_raw(explicit_path: str | None) -> dict:
    path = Path(explicit_path) if explicit_path else _PACKAGED
    if not path.exists():
        raise FileNotFoundError(f"No agent catalog found at {path}.")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache
def load_agent_catalog(explicit_path: str | None = None) -> AgentCatalog:
    raw = _load_raw(explicit_path)
    return AgentCatalog(agents=[AgentSpec(**a) for a in raw["agents"]])
