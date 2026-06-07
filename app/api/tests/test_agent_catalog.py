"""Unit tests for the data-driven agent catalog."""
from __future__ import annotations

from ai4ia_api.agents.agent_catalog import (
    AgentCatalog,
    AgentSpec,
    AgentSummary,
    load_agent_catalog,
)


def test_packaged_catalog_loads_with_expected_agents():
    catalog = load_agent_catalog()
    names = {a.name for a in catalog.agents}
    # The curated starter set.
    assert {"general", "coder", "researcher", "writer", "analyst"} <= names
    # Every agent must carry a non-empty persona prompt.
    for agent in catalog.agents:
        assert agent.systemPrompt.strip(), agent.name
        assert agent.displayName.strip(), agent.name


def test_get_is_case_insensitive_and_misses_return_none():
    catalog = load_agent_catalog()
    assert catalog.get("CODER") is catalog.get("coder")
    assert catalog.get("coder").name == "coder"
    assert catalog.get("does-not-exist") is None
    assert catalog.get("") is None


def test_public_list_excludes_disabled_and_hides_system_prompt():
    catalog = AgentCatalog(
        agents=[
            AgentSpec(
                name="visible",
                displayName="Visible",
                description="shown",
                systemPrompt="secret persona instructions",
            ),
            AgentSpec(
                name="hidden",
                displayName="Hidden",
                description="not shown",
                systemPrompt="other secret",
                enabled=False,
            ),
        ]
    )
    public = catalog.public_list()
    assert [a.name for a in public] == ["visible"]
    # The public projection must be the summary shape (no systemPrompt/tools).
    assert all(isinstance(a, AgentSummary) for a in public)
    assert not hasattr(public[0], "systemPrompt")


def test_enabled_agents_filters_disabled():
    catalog = AgentCatalog(
        agents=[
            AgentSpec(name="a", displayName="A", description="", systemPrompt="p"),
            AgentSpec(
                name="b", displayName="B", description="", systemPrompt="p", enabled=False
            ),
        ]
    )
    assert [a.name for a in catalog.enabled_agents()] == ["a"]
