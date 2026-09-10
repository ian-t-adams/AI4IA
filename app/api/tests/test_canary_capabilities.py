"""Canary compatibility never substitutes for current policy or fresh dispatch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from ai4ia_api.main import create_app
from tests.conftest import make_settings


def test_missing_real_authority_or_v1_is_explicitly_not_ready():
    for enabled in (False, True):
        app = create_app(make_settings(session_deletion_enabled=enabled))
        with TestClient(app) as client:
            result = client.get("/api/canary/capabilities?model=gpt-5.2")
            assert result.status_code == 200
            assert result.headers["cache-control"] == "no-store"
            assert result.json()["ready"] is False
            assert client.get("/api/sessions").json() == []


def test_policy_probe_is_owner_bound_uses_live_eligible_option_and_never_creates_data():
    app = create_app(make_settings(session_deletion_enabled=True))
    with TestClient(app) as client:
        probe = AsyncMock(return_value=SimpleNamespace(outcome="allow"))
        app.state.canary_policy_probe = probe
        result = client.get("/api/canary/capabilities?model=gpt-5.2")
        assert result.status_code == 200
        assert result.json()["ready"] is True
        assert result.json()["constraints"] == {
            "allowTools": False, "allowAutomaticMemory": False, "requireFreshSession": True,
            "maxOutputTokens": 64, "libraryDocumentIds": [],
        }
        user, model, option = probe.call_args.args
        assert user.internal_user_id and model == "gpt-5.2"
        assert option in app.state.catalog.get(model).options
        assert client.get("/api/sessions").json() == []
        probe.return_value = SimpleNamespace(outcome="deny")
        assert client.get("/api/canary/capabilities?model=gpt-5.2").json()["ready"] is False
        probe.return_value = SimpleNamespace(outcome="unavailable")
        assert client.get("/api/canary/capabilities?model=gpt-5.2").json()["ready"] is False
        probe.reset_mock()
        assert client.get("/api/canary/capabilities?model=not-in-catalog").json()["ready"] is False
        probe.assert_not_called()
