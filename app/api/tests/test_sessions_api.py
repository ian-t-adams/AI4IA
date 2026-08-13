"""Tests for the sessions router's tool-override validation.

Focused on ``_conversation_addable_tools``: it unions the static attachable
allowlist with the caller's BYO + official MCP tool names, and must fail
closed (never 500) when either MCP source errors.
"""
from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from ai4ia_api.sessions.cosmos_repo import CosmosSessionRepository
from ai4ia_api.sessions.models import Session
from ai4ia_api.routers.sessions import (
    MAX_SESSION_SYSTEM_PROMPT_CHARS,
    CreateSessionRequest,
)


class _RaisingMcpService:
    async def list_for(self, user_id: str):
        raise RuntimeError("cosmos is unavailable")


class _RaisingOfficialMcpService:
    async def list_all(self):
        raise RuntimeError("discovery endpoint timed out")


class _LegacyDocContainer:
    """Cosmos container stand-in serving one already-stored session document
    whose ``toolOverrides`` predates/violates the current schema (e.g.
    written before the field existed, or hand-edited)."""

    def __init__(self, item: dict) -> None:
        self.item = item

    async def read_item(self, *, item, partition_key):
        return dict(self.item)

    async def patch_item(
        self, *, item, partition_key, patch_operations, etag=None, match_condition=None
    ):
        for operation in patch_operations:
            self.item[operation["path"].lstrip("/")] = operation["value"]
        self.item["_etag"] = "e2"
        return dict(self.item)


def _create(client, **overrides):
    body = {"title": "Chat"}
    body.update(overrides)
    return client.post("/api/sessions", json=body)


def test_session_system_prompt_has_request_boundary():
    assert MAX_SESSION_SYSTEM_PROMPT_CHARS == 8000
    request = CreateSessionRequest(
        systemPrompt="x" * 8000
    )
    assert request.systemPrompt is not None
    with pytest.raises(ValidationError):
        CreateSessionRequest(systemPrompt="x" * 8001)


def test_create_session_accepts_static_tool_override(client):
    resp = _create(client, toolOverrides={"added": ["calculator"], "removed": []})
    assert resp.status_code == 201, resp.text


def test_session_image_preferences_are_catalog_validated_and_patchable(client):
    created = _create(
        client,
        imagePreferences={
            "models": ["FLUX.2-pro", "gpt-image-2"],
            "size": "1024x1024",
            "quality": "auto",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["imagePreferences"] == {
        "models": ["FLUX.2-pro", "gpt-image-2"],
        "size": "1024x1024",
        "quality": "auto",
    }

    updated = client.patch(
        f"/api/sessions/{created.json()['id']}",
        json={
            "imagePreferences": {
                "models": ["FLUX.1-Kontext-pro"],
                "size": "1024x1024",
                "quality": "auto",
            }
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["imagePreferences"]["models"] == ["FLUX.1-Kontext-pro"]


@pytest.mark.parametrize(
    "preferences",
    [
        {"models": ["gpt-5.2"]},
        {"models": ["FLUX.1-Kontext-pro"], "size": "1536x1024"},
        {"models": ["FLUX.2-pro"], "quality": "high"},
        {"models": [], "size": "1024x1024"},
        {
            "models": [
                "FLUX.2-pro",
                "FLUX.2-flex",
                "gpt-image-2",
                "gpt-image-1.5",
            ]
        },
    ],
)
def test_session_rejects_invalid_image_preferences(client, preferences):
    response = _create(client, imagePreferences=preferences)
    assert response.status_code == 422


def test_session_patch_revalidates_image_preferences_before_persisting(client):
    created = _create(client)
    session_id = created.json()["id"]
    invalid = {
        "models": ["gpt-5.2"],
        "size": "1024x1024",
        "quality": "auto",
    }

    response = client.patch(
        f"/api/sessions/{session_id}",
        json={"imagePreferences": invalid},
    )

    assert response.status_code == 422
    persisted = client.get(f"/api/sessions/{session_id}").json()
    assert persisted["imagePreferences"] == {
        "models": [],
        "size": None,
        "quality": None,
    }


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


def test_patch_non_tool_field_survives_corrupted_persisted_tool_overrides(client):
    """Production regression: PATCH /api/sessions/<id> returned 500 whenever
    a persisted document's ``toolOverrides`` no longer matched the schema
    (e.g. written before the field existed, or hand-edited). ``update_session``
    falls back to ``session.toolOverrides`` for any PATCH that doesn't touch
    the field itself, and the read that produces it — ``Session.model_validate``
    inside the Cosmos repository's ``_owned_session`` — used to raise an
    uncaught ``ValidationError`` for a malformed value, turning every future
    GET/PATCH of that one session into a permanent 500. This drives the real
    ``PATCH`` endpoint (not just the repository or model directly) against a
    Cosmos-shaped document with a corrupted ``toolOverrides``.
    """
    created = _create(client)
    assert created.status_code == 201, created.text
    session = Session.model_validate(created.json())

    doc = {**session.model_dump(mode="json"), "toolOverrides": None, "_etag": "e1"}
    repo = object.__new__(CosmosSessionRepository)
    repo._sessions = _LegacyDocContainer(doc)
    client.app.state.session_repo = repo

    resp = client.patch(f"/api/sessions/{session.id}", json={"title": "Renamed"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Renamed"
    assert body["toolOverrides"] == {"added": [], "removed": []}


def test_patch_non_tool_field_preserves_existing_tool_overrides(client):
    """A PATCH that only changes an unrelated field (title) must not disturb
    already-configured, well-formed tool overrides."""
    created = _create(
        client, toolOverrides={"added": ["calculator"], "removed": []}
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    resp = client.patch(f"/api/sessions/{session_id}", json={"title": "Renamed"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Renamed"
    assert body["toolOverrides"] == {"added": ["calculator"], "removed": []}
