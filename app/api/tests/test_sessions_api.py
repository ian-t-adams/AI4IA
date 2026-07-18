"""Tests for the sessions router's tool-override validation.

Focused on ``_conversation_addable_tools``: it unions the static attachable
allowlist with the caller's BYO + official MCP tool names, and must fail
closed (never 500) when either MCP source errors.
"""
from __future__ import annotations

import logging


class _RaisingMcpService:
    async def list_for(self, user_id: str):
        raise RuntimeError("cosmos is unavailable")


class _RaisingOfficialMcpService:
    async def list_all(self):
        raise RuntimeError("discovery endpoint timed out")


def _create(client, **overrides):
    body = {"title": "Chat"}
    body.update(overrides)
    return client.post("/api/sessions", json=body)


def test_create_session_accepts_static_tool_override(client):
    resp = _create(client, toolOverrides={"added": ["calculator"], "removed": []})
    assert resp.status_code == 201, resp.text


def test_create_session_rejects_unknown_tool_override(client):
    resp = _create(client, toolOverrides={"added": ["no-such-tool"], "removed": []})
    assert resp.status_code == 422, resp.text
    assert "no-such-tool" in resp.json()["detail"]


def test_mcp_lookup_failure_fails_closed_and_logs(client, caplog):
    # A BYO MCP tool can never be validated as addable while the store used to
    # discover it is erroring, so the override must be rejected (not a 500) —
    # and the failure must be logged, not swallowed silently.
    client.app.state.mcp_service = _RaisingMcpService()
    with caplog.at_level(logging.WARNING, logger="ai4ia_api.routers.sessions"):
        resp = _create(
            client, toolOverrides={"added": ["mcp:weather/get_forecast"], "removed": []}
        )
    assert resp.status_code == 422, resp.text
    assert "mcp:weather/get_forecast" in resp.json()["detail"]
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "mcp tool-name resolution failed" in messages

    # The static allowlist is unaffected by the BYO MCP outage.
    ok = _create(client, toolOverrides={"added": ["calculator"], "removed": []})
    assert ok.status_code == 201, ok.text


def test_official_mcp_lookup_failure_fails_closed_and_logs(client, caplog):
    client.app.state.official_mcp_service = _RaisingOfficialMcpService()
    with caplog.at_level(logging.WARNING, logger="ai4ia_api.routers.sessions"):
        resp = _create(
            client, toolOverrides={"added": ["mcp:curated/search"], "removed": []}
        )
    assert resp.status_code == 422, resp.text
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "official mcp tool-name resolution failed" in messages
