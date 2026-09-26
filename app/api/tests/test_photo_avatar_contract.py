"""The published photo avatar HTTP contract: routes, shapes, status codes and codes.

Layer 2 (the web gallery) builds against these field names. A rename here must
fail a test, so the exact key sets are pinned rather than spot-checked.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.main import create_app
from ai4ia_api.photo_avatars.models import (
    ATTESTATION_VERSION,
    PhotoAvatar,
    PhotoAvatarAttributeOptions,
    PhotoAvatarAttributes,
    PhotoAvatarConfig,
    PhotoAvatarCost,
    PhotoAvatarError,
    PhotoAvatarLimits,
    PhotoAvatarList,
    PhotoAvatarPricing,
    PhotoAvatarPreview,
    PhotoAvatarReportReceipt,
    attestation_info,
    disclosure_info,
    feedback_info,
)
from tests.conftest import make_settings

RECORD_ID = "0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
VALID_BODY = {
    "displayName": "Friendly host",
    "prompt": "A friendly virtual host, head and shoulders, plain background.",
    "gender": "Female",
    "style": "Realistic",
    "attestation": {
        "version": ATTESTATION_VERSION, "fictional": True, "adult": True, "notRealPerson": True,
    },
}

AVATAR_KEYS = {
    "id", "displayName", "prompt", "attributes", "status", "failure", "preview",
    "disclosure", "cost", "usable", "reported", "needsReverification", "createdAt", "updatedAt",
    "readyAt",
}
CONFIG_KEYS = {
    "enabled", "available", "reason", "canCreate", "limits", "attributes",
    "attestation", "disclosure", "pricing", "feedback",
}


def _avatar(**overrides) -> PhotoAvatar:
    values = dict(
        id=RECORD_ID, displayName="Friendly host", prompt=VALID_BODY["prompt"],
        attributes=PhotoAvatarAttributes(gender="Female", style="Realistic"),
        status="ready",
        preview=PhotoAvatarPreview(
            url=f"/api/photo-avatars/{RECORD_ID}/preview", width=1024, height=1024, bytes=len(PNG),
        ),
        cost=PhotoAvatarCost(estimatedUsd=2.0, known=True, priceVersion="fixture-v1"),
        usable=True, createdAt=NOW, updatedAt=NOW, readyAt=NOW,
    )
    values.update(overrides)
    return PhotoAvatar(**values)


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.refusal: PhotoAvatarError | None = None

    def _check(self, name: str, user) -> None:
        self.calls.append((name, user.internal_user_id))
        if self.refusal is not None:
            raise self.refusal

    async def config(self, user):
        self._check("config", user)
        return PhotoAvatarConfig(
            enabled=True, available=True, reason="available", canCreate=True,
            limits=PhotoAvatarLimits(
                maxAvatars=5, avatarCount=1, maxCreationsPerDay=5, creationsInLastDay=1,
                promptMaxChars=1000,
            ),
            attributes=PhotoAvatarAttributeOptions(
                gender=["Male", "Female"], age=["YoungAdult"], ethnicity=["Asian"], style=["Realistic"],
            ),
            attestation=attestation_info(),
            disclosure=disclosure_info(),
            pricing=PhotoAvatarPricing(
                currency="USD", estimatedUsdPerAvatar=2.0, known=True, priceVersion="fixture-v1",
            ),
            feedback=feedback_info(),
        )

    async def list(self, user):
        self._check("list", user)
        return PhotoAvatarList(avatars=[_avatar()])

    async def create(self, user, request):
        self._check("create", user)
        return _avatar(status="generating", preview=None, usable=False, readyAt=None)

    async def get(self, user, avatar_id):
        self._check("get", user)
        return _avatar()

    async def preview(self, user, avatar_id):
        self._check("preview", user)
        return PNG

    async def delete(self, user, avatar_id):
        self._check("delete", user)

    async def report(self, user, avatar_id, request):
        self._check("report", user)
        return PhotoAvatarReportReceipt(
            id="report-0123456789abcdef", avatarId=avatar_id, reason=request.reason, createdAt=NOW,
        )


def _client(enabled: bool) -> tuple[TestClient, FakeService]:
    app = create_app(make_settings(photo_avatars_enabled=enabled))
    client = TestClient(app)
    client.__enter__()
    fake = FakeService()
    app.state.photo_avatars = fake
    return client, fake


ROUTES = [
    ("get", "/api/photo-avatars", None),
    ("post", "/api/photo-avatars", VALID_BODY),
    ("get", f"/api/photo-avatars/{RECORD_ID}", None),
    ("get", f"/api/photo-avatars/{RECORD_ID}/preview", None),
    ("delete", f"/api/photo-avatars/{RECORD_ID}", None),
    ("post", f"/api/photo-avatars/{RECORD_ID}/reports", {"reason": "other"}),
]


def test_settings_default_off():
    assert make_settings().photo_avatars_enabled is False


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_every_route_but_config_is_404_while_disabled_and_reaches_the_service_when_on(
    method, path, body,
):
    disabled, disabled_fake = _client(False)
    try:
        response = getattr(disabled, method)(path, **({"json": body} if body else {}))
        assert response.status_code == 404
        assert response.json()["code"] == "photo_avatars_disabled"
        assert disabled_fake.calls == []
    finally:
        disabled.__exit__(None, None, None)
    # Control: the identical call reaches the service once the flag is on.
    enabled, enabled_fake = _client(True)
    try:
        response = getattr(enabled, method)(path, **({"json": body} if body else {}))
        assert response.status_code in {200, 202, 204}, response.text
        assert len(enabled_fake.calls) == 1
    finally:
        enabled.__exit__(None, None, None)


def test_config_is_always_200_and_reports_disabled_without_detail():
    client, fake = _client(False)
    try:
        body = client.get("/api/photo-avatars/config").json()
    finally:
        client.__exit__(None, None, None)
    assert set(body) == CONFIG_KEYS
    assert body["enabled"] is False and body["available"] is False
    assert body["reason"] == "disabled" and body["canCreate"] is False
    assert all(body[key] is None for key in CONFIG_KEYS - {"enabled", "available", "reason", "canCreate"})
    assert fake.calls == []


def test_enabled_shapes_are_the_published_contract():
    client, _ = _client(True)
    try:
        config = client.get("/api/photo-avatars/config").json()
        created = client.post("/api/photo-avatars", json=VALID_BODY)
        listed = client.get("/api/photo-avatars").json()
        one = client.get(f"/api/photo-avatars/{RECORD_ID}").json()
        report = client.post(
            f"/api/photo-avatars/{RECORD_ID}/reports", json={"reason": "impersonation", "details": "x"},
        )
    finally:
        client.__exit__(None, None, None)
    assert set(config) == CONFIG_KEYS
    assert set(config["limits"]) == {
        "maxAvatars", "avatarCount", "maxCreationsPerDay", "creationsInLastDay",
        "nextCreationAt", "promptMaxChars", "displayNameMaxChars",
    }
    assert set(config["attributes"]) == {"gender", "age", "ethnicity", "style"}
    assert config["attestation"]["version"] == ATTESTATION_VERSION
    assert [item["id"] for item in config["attestation"]["statements"]] == [
        "fictional", "adult", "notRealPerson",
    ]
    assert set(config["disclosure"]) == {"label", "text"}
    assert set(config["pricing"]) == {"currency", "estimatedUsdPerAvatar", "known", "priceVersion"}
    assert set(config["feedback"]) == {"reasons", "detailsMaxChars", "microsoftReportUrl"}
    assert created.status_code == 202
    assert set(created.json()) == AVATAR_KEYS
    assert created.json()["status"] == "generating"
    assert set(listed) == {"avatars"} and set(listed["avatars"][0]) == AVATAR_KEYS
    assert set(one) == AVATAR_KEYS
    assert set(one["attributes"]) == {"gender", "age", "ethnicity", "style"}
    assert set(one["preview"]) == {"url", "contentType", "width", "height", "bytes"}
    assert one["disclosure"] == {"aiGenerated": True, "label": "AI-generated"}
    assert set(one["cost"]) == {"currency", "estimatedUsd", "known", "priceVersion", "basis"}
    assert report.status_code == 202
    assert set(report.json()) == {"id", "avatarId", "reason", "createdAt"}
    # The provider-side identifier is never part of any response.
    for payload in (created.json(), one, listed):
        assert "provider" not in str(payload).lower()


def test_preview_is_png_with_disclosure_and_private_caching():
    client, _ = _client(True)
    try:
        response = client.get(f"/api/photo-avatars/{RECORD_ID}/preview")
    finally:
        client.__exit__(None, None, None)
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-ai4ia-synthetic-media"] == "ai-generated"


@pytest.mark.parametrize("bad", ["not-hex", RECORD_ID.upper(), RECORD_ID + "0", "ai4ia-0123456789abcdef0123"])
def test_malformed_record_ids_are_404_before_the_service(bad):
    client, fake = _client(True)
    try:
        responses = [
            client.get(f"/api/photo-avatars/{bad}"),
            client.get(f"/api/photo-avatars/{bad}/preview"),
            client.delete(f"/api/photo-avatars/{bad}"),
            client.post(f"/api/photo-avatars/{bad}/reports", json={"reason": "other"}),
        ]
        control = client.get(f"/api/photo-avatars/{RECORD_ID}")
    finally:
        client.__exit__(None, None, None)
    assert [response.status_code for response in responses] == [404] * 4
    assert all(response.json()["code"] == "not_found" for response in responses)
    assert fake.calls == [("get", fake.calls[0][1])]
    assert control.status_code == 200


@pytest.mark.parametrize(
    "mutation",
    [
        {"attestation": {**VALID_BODY["attestation"], "fictional": False}},
        {"attestation": {**VALID_BODY["attestation"], "adult": False}},
        {"attestation": {**VALID_BODY["attestation"], "notRealPerson": False}},
        {"attestation": None},
        {"displayName": ""},
        {"displayName": "x" * 61},
        {"prompt": ""},
        {"providerAvatarId": "ai4ia-0123456789abcdef0123"},
        {"description": "not part of the contract"},
    ],
)
def test_schema_invalid_creates_are_422_before_the_service(mutation):
    client, fake = _client(True)
    try:
        body = {**VALID_BODY, **mutation}
        response = client.post("/api/photo-avatars", json=body)
        control = client.post("/api/photo-avatars", json=VALID_BODY)
    finally:
        client.__exit__(None, None, None)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert control.status_code == 202
    assert [name for name, _ in fake.calls] == ["create"]


def test_refusals_render_code_reason_and_retry_after():
    client, fake = _client(True)
    try:
        fake.refusal = PhotoAvatarError(
            503, "photo_avatars_unavailable", "Photo avatars are unavailable.",
            reason="capability_unavailable",
        )
        unavailable = client.post("/api/photo-avatars", json=VALID_BODY)
        fake.refusal = PhotoAvatarError(
            429, "daily_creation_limit", "Daily creation limit reached.", retry_after=321,
        )
        limited = client.post("/api/photo-avatars", json=VALID_BODY)
    finally:
        client.__exit__(None, None, None)
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "photo_avatars_unavailable"
    assert unavailable.json()["reason"] == "capability_unavailable"
    assert limited.status_code == 429
    assert limited.json()["code"] == "daily_creation_limit"
    assert limited.headers["retry-after"] == "321"
    assert "reason" not in limited.json()
