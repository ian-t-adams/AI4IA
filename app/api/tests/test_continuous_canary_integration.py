"""The actual monitor crosses signed auth, policy, claim, model transport and v1 cleanup."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.test_policy_execution_profiles import MONITOR, profiles as profiles


@pytest.mark.parametrize("ready", [False, True])
async def test_monitor_runs_only_through_real_policy_factory_and_owned_v1_lifecycle(profiles, monkeypatch, ready):
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root))
    from scripts.canaries.configuration import Configuration
    from scripts.canaries.contracts import Report, Run, encoded, stamp
    from scripts.canaries.monitor import chat
    from scripts.canaries.transport import Response

    client, _model, headers, provider_calls = profiles
    policy = json.loads(client.app.state.settings.group_policy_json)
    policy["domains"]["models"]["default"]["allow"] = ["chat", "chat-fast"]
    client.app.state.settings.group_policy_json = json.dumps(policy)
    if not ready:
        client.app.state.canary_dispatch_guard = None
    requests = []

    class ApplicationTransport:
        async def request(self, method, url, *, token=None, body=None, **_):
            requests.append((method, url))
            parsed = urlsplit(url)
            assert parsed.netloc == "web.example.test"
            result = client.request(
                method, parsed.path + (f"?{parsed.query}" if parsed.query else ""),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                content=body, follow_redirects=False,
            )
            return Response(
                result.status_code, result.content, 0.01,
                result.headers.get("content-type", "").split(";", 1)[0],
            )

    now = datetime.now(timezone.utc)
    config = Configuration(
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222", MONITOR,
        "api://44444444-4444-4444-4444-444444444444",
        "https://web.example.test", "https://api.example.test",
        "55555555-5555-5555-5555-555555555555", stamp(now + timedelta(hours=1)),
        4, 21600, True, True, True, False,
    )
    report = Report(Run("owner/repo", 1, 200, 2, 1, "a" * 40), stamp(now))
    source = json.loads((root / "infra" / "models.json").read_text(encoding="utf-8"))
    token = headers(MONITOR)["Authorization"].removeprefix("Bearer ")
    await chat(ApplicationTransport(), config, token, source, report)
    if not ready:
        assert provider_calls == []
        assert not any(method != "GET" for method, _ in requests)
        assert report.stages["posture"].outcome != "pass"
    else:
        assert len(provider_calls) == 1, report.document()
        assert all(report.stages[name].outcome == "pass" for name in (
            "platform", "auth", "catalog", "posture", "session", "gateway", "model", "persistence", "cleanup",
        )), report.document()
        assert report.cleanup_safe
        assert report.usage_known and report.estimated_micro_usd is not None
        assert client.get("/api/sessions", headers=headers(MONITOR)).json() == []
        assert len(client.get("/api/sessions/deletions", headers=headers(MONITOR)).json()["items"]) == 1
        assert any("selection=least_estimated_cost" in url for _, url in requests)
    assert token not in encoded(report.document()).decode()
    assert MONITOR not in encoded(report.document()).decode()
