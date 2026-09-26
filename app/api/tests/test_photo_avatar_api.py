"""End-to-end photo avatar API: real router, service, store, ledger and preview checks.

The provider is a synthetic fake behind the governed route shape; no real
identifiers, accounts or links appear here. Every guard is paired with a
control that shows the same path succeeding when the guarded condition flips.
"""
from __future__ import annotations

import json
import logging
import re
import struct
import zlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.auth.userid import internal_user_id
from ai4ia_api.entitlements.models import EntitlementLimits
from ai4ia_api.hard_quota.models import QuotaError
from ai4ia_api.library.blob_store import InMemoryBlobStore
from ai4ia_api.main import create_app
from ai4ia_api.photo_avatars import service as service_module
from ai4ia_api.photo_avatars.availability import CapabilityProbe
from ai4ia_api.photo_avatars.catalog import load_photo_avatar_catalog
from ai4ia_api.photo_avatars.models import ATTESTATION_VERSION
from ai4ia_api.photo_avatars.preview import PhotoAvatarArtifactStore, fetch_preview
from ai4ia_api.photo_avatars.provider import PhotoAvatarGateway
from ai4ia_api.photo_avatars.service import CONFIRM_GRACE, PhotoAvatarService
from ai4ia_api.photo_avatars.store import LEDGER_ID, PhotoAvatarStore
from ai4ia_api.publishing.store import InMemoryRecordStore
from ai4ia_api.usage.models import PHOTO_AVATAR_PROVIDER
from ai4ia_api.usage.pricing import PricingBook
from tests.conftest import make_settings

CATALOG = load_photo_avatar_catalog()
HOST = CATALOG.preview.host
TOKEN = "SYNTHETICSASSIGNATURE0123456789abc"
T0 = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
NOT_FOUND = {"error": {"code": "NotFound", "message": "Synthetic avatar not found."}}
BODY = {
    "displayName": "Friendly host",
    "prompt": "A friendly virtual host, head and shoulders, plain background.",
    "gender": "Female",
    "style": "Realistic",
    "attestation": {
        "version": ATTESTATION_VERSION, "fictional": True, "adult": True, "notRealPerson": True,
    },
}


def png(width: int = 1024, height: int = 1024) -> bytes:
    chunk = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk))


def owner(name: str) -> str:
    return internal_user_id(provider="dev", issuer="ai4ia-dev", subject=name, tenant_id="dev")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Provider:
    """A synthetic provider behind ``/ai4ia-photo-avatars-v1``."""

    def __init__(self) -> None:
        self.features: object = ["CustomAvatar"]
        self.features_status = 200
        self.project = True
        self.avatars: dict[str, dict] = {}
        self.create_reply: httpx.Response | Exception | None = None
        self.accept_despite_reply = False
        self.delete_status: int | None = None
        self.preview_host = HOST
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = request.url.path.removeprefix("/ai4ia-photo-avatars-v1")
        assert route != request.url.path, request.url.path
        if route == "/features":
            return httpx.Response(self.features_status, json=self.features)
        if route == "/project":
            if request.method == "GET":
                return httpx.Response(200, json={"kind": "PhotoAvatar"}) if self.project else httpx.Response(
                    404, json=NOT_FOUND,
                )
            assert request.content == b""
            self.project = True
            return httpx.Response(201, json={"kind": "PhotoAvatar"})
        match = re.fullmatch(r"/photoavatars/(ai4ia-[0-9a-f]{20})", route)
        assert match, route
        avatar_id = match.group(1)
        if request.method == "PUT":
            body = json.loads(request.content)
            if self.create_reply is not None:
                if self.accept_despite_reply:
                    self.avatars[avatar_id] = {"state": "Running", "body": body}
                if isinstance(self.create_reply, Exception):
                    raise self.create_reply
                return self.create_reply
            self.avatars[avatar_id] = {"state": "Running", "body": body}
            return httpx.Response(201, json={"id": avatar_id, "state": "NotStarted", **body})
        if request.method == "GET":
            avatar = self.avatars.get(avatar_id)
            if avatar is None:
                return httpx.Response(404, json=NOT_FOUND)
            payload = {"id": avatar_id, "state": avatar["state"], **avatar["body"]}
            if avatar["state"] == "Succeeded":
                payload["promptImageUri"] = (
                    f"https://{self.preview_host}/avatars/{avatar_id}/x/y/z?sv=2025&sp=r&sig={TOKEN}"
                )
            return httpx.Response(200, json=payload)
        assert request.method == "DELETE"
        if self.delete_status is not None:
            return httpx.Response(self.delete_status)
        return httpx.Response(204) if self.avatars.pop(avatar_id, None) else httpx.Response(404, json=NOT_FOUND)

    def creates(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "PUT" and "/photoavatars/" in r.url.path]

    def deletes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "DELETE"]

    def finish(self, state: str = "Succeeded") -> None:
        for avatar in self.avatars.values():
            avatar["state"] = state


class Blob(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.body = png()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=self.body, headers={"content-type": "application/octet-stream"})


class Harness:
    def __init__(self, **settings) -> None:
        values = dict(photo_avatars_enabled=True, model_gateway_url="https://proxy.test/openai")
        values.update(settings)
        self.app = create_app(make_settings(**values))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.provider, self.blob, self.clock = Provider(), Blob(), Clock()
        self.records, self.previews = InMemoryRecordStore(), InMemoryBlobStore()
        config = self.app.state.settings
        gateway = PhotoAvatarGateway(config, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(self.provider), follow_redirects=False,
        ))

        async def fetcher(link, preview):
            return await fetch_preview(
                link, preview, resolver=lambda host: ["20.60.1.10"], inner_transport=self.blob,
            )

        self.service = PhotoAvatarService(
            settings=config, catalog=CATALOG, store=PhotoAvatarStore(self.records),
            artifacts=PhotoAvatarArtifactStore(self.previews), gateway=gateway,
            capability=CapabilityProbe(gateway, CATALOG.requiredFeature),
            entitlements=self.app.state.entitlements, usage=self.app.state.usage,
            pricing=self.app.state.usage.pricing, clock=self.clock, preview_fetcher=fetcher,
            policy=self.app.state.policy,
        )
        self.app.state.photo_avatars = self.service

    def close(self) -> None:
        self.client.__exit__(None, None, None)

    def call(self, method: str, path: str = "", user: str = "alice", **kwargs) -> httpx.Response:
        return self.client.request(
            method, f"/api/photo-avatars{path}", headers={"X-Dev-User": user}, **kwargs,
        )

    def create(self, user: str = "alice", **overrides) -> httpx.Response:
        return self.call("POST", user=user, json={**BODY, **overrides})

    def poll(self, avatar_id: str, user: str = "alice", seconds: float = 5) -> dict:
        self.clock.advance(seconds)
        response = self.call("GET", f"/{avatar_id}", user=user)
        assert response.status_code == 200, response.text
        return response.json()

    def usage_rows(self) -> list:
        repo = self.app.state.usage._repo
        return [row for rows in repo._by_user.values() for row in rows if row.provider == PHOTO_AVATAR_PROVIDER]

    def stored_bodies(self) -> str:
        return json.dumps([snapshot.body for snapshot in self.records.items.values()], default=str)


@pytest.fixture
def h():
    harness = Harness()
    yield harness
    harness.close()


def test_the_service_exists_only_while_the_flag_is_on():
    off = create_app(make_settings())
    with TestClient(off):
        assert off.state.photo_avatars is None
    on = create_app(make_settings(photo_avatars_enabled=True))
    with TestClient(on):
        assert on.state.photo_avatars is not None


def test_create_generate_ready_preview_list_and_meter_once(h, caplog):
    root = logging.getLogger()
    root.addHandler(caplog.handler)
    try:
        config = h.call("GET", "/config").json()
        assert config["available"] is True and config["reason"] == "available" and config["canCreate"]
        assert config["pricing"] == {
            "currency": "USD", "estimatedUsdPerAvatar": 2.0, "known": True,
            "priceVersion": h.app.state.usage.pricing.version,
        }
        created = h.create()
        assert created.status_code == 202, created.text
        avatar = created.json()
        assert avatar["status"] == "generating" and avatar["cost"]["known"] is True
        (put,) = h.provider.creates()
        assert json.loads(put.content) == {"properties": {
            "prompt": BODY["prompt"], "gender": "Female", "style": "Realistic",
        }}
        assert h.poll(avatar["id"])["status"] == "generating"
        h.provider.finish()
        ready = h.poll(avatar["id"])
        assert ready["status"] == "ready" and ready["usable"] is True
        assert ready["preview"] == {
            "url": f"/api/photo-avatars/{avatar['id']}/preview", "contentType": "image/png",
            "width": 1024, "height": 1024, "bytes": len(h.blob.body),
        }
        assert ready["disclosure"] == {"aiGenerated": True, "label": "AI-generated"}
        image = h.call("GET", f"/{avatar['id']}/preview")
        assert image.status_code == 200 and image.content == h.blob.body
        assert len(h.blob.requests) == 1
        # Later reads serve the stored copy; the provider link is never fetched again.
        h.poll(avatar["id"])
        assert len(h.blob.requests) == 1
        listed = h.call("GET").json()["avatars"]
        assert [item["id"] for item in listed] == [avatar["id"]] and listed[0]["usable"] is True
        (row,) = h.usage_rows()
        assert row.id == f"photo-avatar-create-{avatar['id']}"
        assert row.costKnown is True and row.estCostMicroUsd == 2_000_000
        assert row.billingUnit == "avatar" and row.providerCompleted is True
        # The signed link reached the fetch and nowhere else.
        assert TOKEN in str(h.blob.requests[0].url)
        for payload in (created.text, json.dumps(ready), json.dumps(listed), h.stored_bodies()):
            assert TOKEN not in payload and HOST not in payload
        messages = [record.getMessage() for record in caplog.records]
        assert any("photo avatar create outcome=accepted" in message for message in messages)
        assert all(TOKEN not in message for message in messages)
    finally:
        root.removeHandler(caplog.handler)


def test_another_user_can_neither_read_preview_report_nor_delete(h):
    avatar = h.create().json()
    h.provider.finish()
    assert h.poll(avatar["id"])["status"] == "ready"
    for method, path, kwargs in (
        ("GET", f"/{avatar['id']}", {}),
        ("GET", f"/{avatar['id']}/preview", {}),
        ("POST", f"/{avatar['id']}/reports", {"json": {"reason": "other"}}),
    ):
        assert h.call(method, path, user="bob", **kwargs).status_code == 404
    assert h.call("GET", user="bob").json() == {"avatars": []}
    assert h.call("DELETE", f"/{avatar['id']}", user="bob").status_code == 204
    assert h.provider.deletes() == []
    # Control: the owner still sees, previews and can delete the untouched avatar.
    assert h.call("GET", f"/{avatar['id']}/preview").status_code == 200
    assert h.call("DELETE", f"/{avatar['id']}").status_code == 204
    assert len(h.provider.deletes()) == 1


@pytest.mark.parametrize("features, status, reason", [
    (["BuiltInVoicePreview"], 200, "capability_unavailable"),
    (["CustomAvatarPreview", "customavatar"], 200, "capability_unavailable"),
    ({"CustomAvatar": True}, 200, "capability_unknown"),
    (["CustomAvatar"], 500, "capability_unknown"),
])
def test_creation_fails_closed_without_the_exact_capability(features, status, reason):
    harness = Harness()
    try:
        harness.provider.features, harness.provider.features_status = features, status
        assert harness.call("GET", "/config").json()["reason"] == reason
        refused = harness.create()
        assert refused.status_code == 503 and refused.json()["reason"] == reason
        assert harness.provider.creates() == []
        # Control: the exact feature, once the short cache expires, admits creation.
        harness.provider.features, harness.provider.features_status = ["CustomAvatar"], 200
        harness.service._capability.invalidate()
        assert harness.create().status_code == 202
        assert len(harness.provider.creates()) == 1
    finally:
        harness.close()


def test_losing_the_capability_keeps_existing_avatars_readable_and_deletable(h):
    avatar = h.create().json()
    h.provider.finish()
    assert h.poll(avatar["id"])["usable"] is True
    h.provider.features = []
    h.service._capability.invalidate()
    view = h.poll(avatar["id"])
    assert view["status"] == "ready" and view["usable"] is False
    assert h.call("GET", f"/{avatar['id']}/preview").status_code == 200
    assert h.create().status_code == 503
    assert h.call("DELETE", f"/{avatar['id']}").status_code == 204


def test_an_unknown_create_is_confirmed_by_status_reads_and_never_resent(h):
    h.provider.create_reply = httpx.Response(503)
    h.provider.accept_despite_reply = True  # the response was lost, the create landed
    created = h.create().json()
    assert created["status"] == "confirming"
    assert len(h.provider.creates()) == 1
    (row,) = h.usage_rows()
    assert row.costKnown is False and row.providerCompleted is False  # unknown, not free
    h.provider.create_reply = None
    confirmed = h.poll(created["id"])
    assert confirmed["status"] == "generating"
    h.provider.finish()
    assert h.poll(created["id"])["status"] == "ready"
    assert len(h.provider.creates()) == 1
    # The first ledger row stands; acceptance found later does not reprice it.
    (row,) = h.usage_rows()
    assert row.costKnown is False


def test_an_unknown_create_that_never_landed_fails_only_after_the_grace(h):
    h.provider.create_reply = httpx.ReadTimeout("slow")
    created = h.create().json()
    assert created["status"] == "confirming"
    assert h.poll(created["id"], seconds=30)["status"] == "confirming"
    early = h.call("DELETE", f"/{created['id']}")
    assert early.status_code == 409 and early.json()["code"] == "avatar_confirming"
    assert int(early.headers["retry-after"]) > 0
    h.clock.advance(CONFIRM_GRACE.total_seconds())
    failed = h.poll(created["id"])
    assert failed["status"] == "failed" and failed["failure"]["code"] == "not_created"
    assert len(h.provider.creates()) == 1
    assert h.call("DELETE", f"/{created['id']}").status_code == 204


@pytest.mark.parametrize("reply, code", [
    (httpx.Response(400, json={"error": {"code": "InvalidPrompt"}}), "provider_rejected"),
    (httpx.Response(429), "provider_throttled"),
    (httpx.Response(403), "provider_forbidden"),
])
def test_definite_rejections_fail_without_metering(reply, code):
    harness = Harness()
    try:
        harness.provider.create_reply = reply
        created = harness.create()
        assert created.status_code == 202
        assert created.json()["status"] == "failed" and created.json()["failure"]["code"] == code
        assert len(harness.provider.creates()) == 1
        assert harness.usage_rows() == []
        record = next(iter(s.body for k, s in harness.records.items.items() if k[1] != LEDGER_ID))
        assert record["providerAccepted"] is False
    finally:
        harness.close()


def test_an_unreachable_proxy_creates_nothing_and_spends_no_daily_creation():
    harness = Harness(photo_avatar_max_creations_per_day=1)
    try:
        harness.provider.create_reply = httpx.ConnectError("proxy unreachable")
        refused = harness.create()
        assert refused.status_code == 503
        assert refused.json()["code"] == "photo_avatar_gateway_unavailable"
        assert harness.call("GET").json() == {"avatars": []}
        assert harness.usage_rows() == []
        # Control: the single daily creation is still available afterwards.
        harness.provider.create_reply = None
        assert harness.create().status_code == 202
        assert harness.create().status_code == 429
    finally:
        harness.close()


@pytest.mark.parametrize("priced, capped, enforced, expected", [
    (False, True, True, 503), (True, True, True, 202),
    (False, False, True, 202), (False, True, False, 202),
])
def test_an_unpriced_meter_is_refused_only_under_an_enforced_cost_cap(priced, capped, enforced, expected):
    import asyncio

    harness = Harness(entitlements_enabled=enforced)
    try:
        if not priced:
            harness.service._pricing = PricingBook({}, currency="USD", version="fixture-unpriced")
        if capped:
            asyncio.run(harness.app.state.entitlements.set(
                owner("alice"), EntitlementLimits(costPerDayMicroUsd=50_000_000), updated_by=None,
            ))
        response = harness.create()
        assert response.status_code == expected, response.text
        if expected == 503:
            assert response.json()["code"] == "cost_unknown_under_cap"
            assert harness.provider.creates() == []
        else:
            assert response.json()["cost"]["known"] is priced
            assert len(harness.provider.creates()) == 1
    finally:
        harness.close()


def test_avatar_and_daily_limits_are_strict_and_deleting_frees_only_a_slot():
    harness = Harness(photo_avatar_max_per_user=2, photo_avatar_max_creations_per_day=3)
    try:
        first = harness.create().json()
        harness.create()
        full = harness.create()
        assert full.status_code == 409 and full.json()["code"] == "avatar_limit_reached"
        assert len(harness.provider.creates()) == 2
        assert harness.call("DELETE", f"/{first['id']}").status_code == 204
        assert harness.create().status_code == 202
        harness.call("DELETE", f"/{first['id']}")
        config = harness.call("GET", "/config").json()
        assert config["limits"]["creationsInLastDay"] == 3 and config["canCreate"] is False
        # Both limits are full; the slot limit is checked first.
        full_again = harness.create()
        assert full_again.status_code == 409 and full_again.json()["code"] == "avatar_limit_reached"
        listed = harness.call("GET").json()["avatars"]
        harness.call("DELETE", f"/{listed[0]['id']}")
        daily = harness.create()
        assert daily.status_code == 429 and daily.json()["code"] == "daily_creation_limit"
        assert int(daily.headers["retry-after"]) > 0
        assert len(harness.provider.creates()) == 3
        # Control: the rolling window reopens after a day.
        harness.clock.advance(24 * 3600 + 1)
        assert harness.create().status_code == 202
    finally:
        harness.close()


@pytest.mark.parametrize("overrides, code", [
    ({"gender": "Nonbinary"}, "invalid_photo_avatar_request"),
    ({"style": "Anime"}, "invalid_photo_avatar_request"),
    ({"prompt": "\U0001F600" * 501}, "invalid_photo_avatar_request"),  # 1002 UTF-16 units
    ({"prompt": "A host.\x00"}, "invalid_photo_avatar_request"),
    ({"prompt": "   "}, "invalid_photo_avatar_request"),
    ({"displayName": "Line\nbreak"}, "invalid_photo_avatar_request"),
    ({"attestation": {**BODY["attestation"], "version": "2020-01-01"}}, "attestation_outdated"),
])
def test_validation_is_deterministic_and_precedes_any_provider_call(h, overrides, code):
    refused = h.create(**overrides)
    assert refused.status_code == 422 and refused.json()["code"] == code
    assert h.provider.creates() == []
    assert h.create(prompt="\U0001F600" * 500).status_code == 202  # exactly 1000 units


def test_delete_removes_provider_blob_and_record_idempotently(h):
    avatar = h.create().json()
    h.provider.finish()
    h.poll(avatar["id"])
    key = f"{owner('alice')}/avatars/{avatar['id']}.png"
    assert key in h.previews._data
    h.provider.delete_status = 500
    failed = h.call("DELETE", f"/{avatar['id']}")
    assert failed.status_code == 502 and failed.json()["code"] == "provider_delete_failed"
    assert h.poll(avatar["id"])["status"] == "deleting"
    assert key in h.previews._data
    h.provider.delete_status = None
    assert h.call("DELETE", f"/{avatar['id']}").status_code == 204
    assert key not in h.previews._data
    assert h.call("GET", f"/{avatar['id']}").status_code == 404
    deletes = len(h.provider.deletes())
    assert h.call("DELETE", f"/{avatar['id']}").status_code == 204
    assert len(h.provider.deletes()) == deletes  # nothing left to delete
    ledger = next(s.body for k, s in h.records.items.items() if k[1] == LEDGER_ID)
    assert ledger["active"] == []


def test_a_provider_404_on_delete_counts_as_gone(h):
    avatar = h.create().json()
    h.provider.avatars.clear()
    assert h.call("DELETE", f"/{avatar['id']}").status_code == 204
    assert h.call("GET", f"/{avatar['id']}").status_code == 404


def test_a_lookalike_preview_host_is_rejected_and_nothing_is_stored(h):
    avatar = h.create().json()
    h.provider.preview_host = f"{HOST}.attacker.example"
    h.provider.finish()
    view = h.poll(avatar["id"])
    assert view["status"] == "failed" and view["failure"]["code"] == "preview_rejected"
    assert h.blob.requests == [] and h.previews._data == {}
    assert TOKEN not in h.stored_bodies()
    # Control: the exact host is fetched and stored for a second avatar.
    second = h.create().json()
    h.provider.preview_host = HOST
    h.provider.finish()
    assert h.poll(second["id"])["status"] == "ready"


def test_reports_are_stored_capped_and_leave_the_avatar_intact(h, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_REPORTS_PER_DAY", 2)
    avatar = h.create().json()
    receipt = h.call("POST", f"/{avatar['id']}/reports", json={"reason": "impersonation", "details": " x "})
    assert receipt.status_code == 202 and receipt.json()["reason"] == "impersonation"
    assert h.poll(avatar["id"])["reported"] is True
    reports = [s.body for k, s in h.records.items.items() if k[1].startswith("report-")]
    assert reports[0]["details"] == "x" and reports[0]["ttl"] == 90 * 24 * 3600
    assert h.call("POST", f"/{avatar['id']}/reports", json={"reason": "other"}).status_code == 202
    capped = h.call("POST", f"/{avatar['id']}/reports", json={"reason": "other"})
    assert capped.status_code == 429 and capped.json()["code"] == "report_limit"
    assert h.call("DELETE", f"/{avatar['id']}").status_code == 204
    assert len([k for k in h.records.items if k[1].startswith("report-")]) == 2  # kept for review


def test_a_refusal_before_sending_releases_the_reservation(h, monkeypatch):
    async def refused(*args, **kwargs):
        raise QuotaError("Hard quota requestsPerMinute would be exceeded.", code=429)

    monkeypatch.setattr(h.service._gateway, "create_avatar", refused)
    response = h.create()
    assert response.status_code == 429 and response.json()["code"] == "hard_quota_refused"
    assert h.call("GET").json() == {"avatars": []}
    ledger = next(s.body for k, s in h.records.items.items() if k[1] == LEDGER_ID)
    assert ledger["active"] == [] and ledger["creations"] == []
    assert h.usage_rows() == []


@pytest.mark.parametrize("allowed", [True, False])
def test_the_avatars_policy_domain_gates_creation_and_preview_only(allowed):
    config = {"version": 1, "domains": {"avatars": {"default": {"allow": ["create", "use"] if allowed else []}}}}
    harness = Harness(group_policy_enabled=True, group_policy_json=json.dumps(config))
    try:
        created = harness.create()
        assert created.status_code == (202 if allowed else 403)
        if not allowed:
            assert created.json()["code"] == "policy_denied"
            assert harness.provider.creates() == []
            assert harness.call("GET", "/config").json()["reason"] == "policy_denied"
            # Owner reads, deletion and reports never need a grant.
            assert harness.call("GET").status_code == 200
            assert harness.call("DELETE", "/" + "0" * 32).status_code == 204
        else:
            avatar = created.json()
            harness.provider.finish()
            harness.poll(avatar["id"])
            assert harness.call("GET", f"/{avatar['id']}/preview").status_code == 200
    finally:
        harness.close()


def test_a_user_outside_the_pilot_keeps_their_avatars_visible_and_deletable():
    harness = Harness()
    try:
        avatar = harness.create().json()
        harness.provider.finish()
        assert harness.poll(avatar["id"])["status"] == "ready"
        # Access is withdrawn after creation: only creation and previews stop.
        denied = {"version": 1, "domains": {"avatars": {"default": {"allow": []}}}}
        harness.app.state.settings.group_policy_enabled = True
        harness.app.state.settings.group_policy_json = json.dumps(denied)
        assert harness.call("GET", f"/{avatar['id']}/preview").status_code == 403
        assert harness.create().status_code == 403
        listed = harness.call("GET").json()["avatars"]
        assert [item["id"] for item in listed] == [avatar["id"]] and listed[0]["usable"] is False
        assert harness.call("POST", f"/{avatar['id']}/reports", json={"reason": "other"}).status_code == 202
        assert harness.call("DELETE", f"/{avatar['id']}").status_code == 204
    finally:
        harness.close()
