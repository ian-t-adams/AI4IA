"""Tests for the public GET /api/agents endpoint."""
from __future__ import annotations


def test_agents_endpoint_returns_public_summaries(client):
    resp = client.get("/api/agents")
    assert resp.status_code == 200, resp.text
    agents = resp.json()["agents"]
    names = {a["name"] for a in agents}
    assert {"general", "coder", "researcher"} <= names

    sample = agents[0]
    # Public shape only — internal persona/tool wiring must not be exposed.
    assert set(sample) == {"name", "displayName", "description", "enabled"}
    assert "systemPrompt" not in sample
    assert "tools" not in sample
