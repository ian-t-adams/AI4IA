"""The provider adapter against synthetic contract fixtures (no real identifiers).

Shapes mirror a read-only observation of the creation surface on 2026-09-26.
"""
from __future__ import annotations

import json
import pickle
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest

from ai4ia_api.photo_avatars import provider as provider_module
from ai4ia_api.photo_avatars.provider import (
    API_PATH,
    CREATE_PROXY_TTL_SECONDS,
    CREATE_TIMEOUT_SECONDS,
    FEATURES_TIMEOUT_SECONDS,
    PROVIDER_ID_PATTERN,
    PROVIDER_ID_RULE,
    READ_TIMEOUT_SECONDS,
    PhotoAvatarGateway,
    PreviewLink,
    bounded_error_code,
    build_create_body,
    classify_state,
    new_provider_avatar_id,
    parse_avatar,
    parse_features,
    provider_not_found,
    valid_provider_avatar_id,
)
from tests.conftest import make_settings

REPO = Path(__file__).resolve().parents[3]
SAS = "sig=SYNTHETICSIGNATURE0123456789abcdefABCDEF%3D"
FIXTURE_PREVIEW = f"https://stttssvcproduse2.blob.core.windows.net/c/p/a/b/c/d?sv=2025&sp=r&{SAS}"
AVATAR_FIXTURE = {
    "id": "ai4ia-0123456789abcdef0123",
    "projectId": "fixture-project_PhotoAvatar",
    "state": "Succeeded",
    "createdAt": "2026-09-26T00:00:00Z",
    "lastUpdatedAt": "2026-09-26T00:00:40Z",
    "supportedModels": ["vasa-1"],
    "promptImageUri": FIXTURE_PREVIEW,
    "properties": {"prompt": "A friendly host.", "gender": "Female", "style": "Realistic"},
    "description": "fixture",
}
NOT_FOUND = {"error": {"code": "NotFound", "message": "The avatar fixture was not found."}}


def _settings(**extra):
    values = dict(
        model_gateway_url="https://proxy.test/openai",
        model_gateway_auth_mode="api_key",
        model_gateway_api_key="proxy-ingress-key",
        model_gateway_api_key_header="S7P-KEY",
        outbound_retry_max_attempts=2,
    )
    values.update(extra)
    return make_settings(**values)


class Recorder:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _gateway(recorder: Recorder, **extra) -> PhotoAvatarGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder), follow_redirects=False)
    return PhotoAvatarGateway(_settings(**extra), http_client=client)


def test_issued_ids_are_opaque_short_and_inside_both_patterns():
    ids = {new_provider_avatar_id() for _ in range(500)}
    assert len(ids) == 500
    for value in ids:
        assert PROVIDER_ID_PATTERN.fullmatch(value) and PROVIDER_ID_RULE.fullmatch(value)
        assert len(value) == 26 < 32  # under the receipt redactor's threshold
        assert valid_provider_avatar_id(value)


@pytest.mark.parametrize("value", [
    "sample-foreign-avatar", "ai4ia-0123456789abcdef012", "ai4ia-0123456789ABCDEF0123",
    "ai4ia-0123456789abcdef01234", "../ai4ia-0123456789abcdef0123", "ai4ia-0123456789abcdef012/",
])
def test_foreign_or_malformed_ids_are_not_addressable(value):
    assert not valid_provider_avatar_id(value)
    with pytest.raises(ValueError):
        _gateway(Recorder(httpx.Response(200))).avatar_url(value)


@pytest.mark.parametrize("raw, expected", [
    ("Succeeded", "succeeded"), (" succeeded ", "succeeded"), ("Failed", "failed"),
    ("NotStarted", "pending"), ("Running", "pending"), ("SucceededWithWarnings", "pending"),
    ("", "pending"), (None, "pending"), (1, "pending"),
])
def test_only_an_exact_success_is_success(raw, expected):
    assert classify_state(raw) == expected


def test_avatar_parsing_keeps_state_and_hides_the_preview_link():
    avatar = parse_avatar(AVATAR_FIXTURE)
    assert avatar.state == "succeeded" and avatar.raw_state == "Succeeded"
    assert isinstance(avatar.preview, PreviewLink)
    assert avatar.preview.reveal() == FIXTURE_PREVIEW
    for text in (repr(avatar), str(avatar), repr(avatar.preview), f"{avatar.preview}"):
        assert "SYNTHETICSIGNATURE" not in text
    with pytest.raises(TypeError):
        pickle.dumps(avatar.preview)
    assert parse_avatar({**AVATAR_FIXTURE, "state": "Rendering<script>"}).raw_state is None
    assert parse_avatar(["not", "an", "object"]).state == "pending"


def test_features_and_error_codes_accept_only_the_observed_shapes():
    assert parse_features(["BuiltInVoicePreview", "CustomAvatar"]) == frozenset(
        {"BuiltInVoicePreview", "CustomAvatar"}
    )
    for bad in ({"value": ["CustomAvatar"]}, ["CustomAvatar", 1], "CustomAvatar", None):
        assert parse_features(bad) is None
    assert bounded_error_code(NOT_FOUND) == "NotFound"
    assert bounded_error_code({"code": "InvalidPrompt"}) == "InvalidPrompt"
    assert bounded_error_code({"error": {"code": "has spaces and the prompt"}}) is None
    assert bounded_error_code({"error": {"message": "echoes the prompt"}}) is None


def test_create_body_carries_only_the_prompt_and_listed_attributes():
    body = build_create_body("A host.", {"gender": "Female", "age": None, "ethnicity": None, "style": "Realistic"})
    assert body == {"properties": {"prompt": "A host.", "gender": "Female", "style": "Realistic"}}
    assert "description" not in body


def test_front_paths_match_the_generated_apim_policy():
    gateway = _gateway(Recorder(httpx.Response(200)))
    avatar_id = new_provider_avatar_id()
    assert gateway.root == f"https://proxy.test/{API_PATH}"
    assert gateway.avatar_url(avatar_id) == f"https://proxy.test/{API_PATH}/photoavatars/{avatar_id}"
    policy = (REPO / "infra" / "policies" / "photo-avatars.xml").read_text(encoding="utf-8")
    inner = PROVIDER_ID_PATTERN.pattern.strip("^$")
    assert f"/{API_PATH}/(features|project|photoavatars/({inner}))" in policy
    assert re.search(r"text\.Length &gt; (\d+)", policy)


async def test_create_is_single_attempt_admitted_and_bounded_by_the_proxy_ttl():
    recorder = Recorder(httpx.Response(201, json={**AVATAR_FIXTURE, "state": "NotStarted"}))
    gateway = _gateway(recorder)
    outcome = await gateway.create_avatar(
        "ai4ia-0123456789abcdef0123", build_create_body("A host.", {}), correlation_id="corr-1",
    )
    assert outcome.kind == "accepted" and outcome.avatar.state == "pending"
    (request,) = recorder.requests
    assert request.method == "PUT"
    assert request.url.path == f"/{API_PATH}/photoavatars/ai4ia-0123456789abcdef0123"
    assert not request.url.query
    assert request.headers["S7P-KEY"] == "proxy-ingress-key"
    assert request.headers["S7PTTL"] == str(CREATE_PROXY_TTL_SECONDS)
    assert request.headers["x-correlation-id"] == "corr-1"
    assert "authorization" not in request.headers


@pytest.mark.parametrize("reply, kind, code", [
    (httpx.Response(503), "unknown", None),
    (httpx.Response(500, json={"error": {"code": "InternalError"}}), "unknown", "InternalError"),
    (httpx.Response(502), "unknown", None),
    (httpx.Response(408), "unknown", None),
    (httpx.Response(412), "unknown", None),
    (httpx.ReadTimeout("slow"), "unknown", None),
    (httpx.RemoteProtocolError("reset"), "unknown", None),
    (httpx.ConnectError("refused"), "not_sent", None),
    (httpx.Response(429), "rejected", None),
    (httpx.Response(400, json={"error": {"code": "invalid_photo_avatar_request"}}), "rejected",
     "invalid_photo_avatar_request"),
    (httpx.Response(403), "rejected", None),
])
async def test_a_create_is_never_resent_whatever_happened(reply, kind, code):
    recorder = Recorder(reply)
    outcome = await _gateway(recorder).create_avatar(
        "ai4ia-0123456789abcdef0123", build_create_body("A host.", {}),
    )
    assert outcome.kind == kind and outcome.error_code == code
    assert len(recorder.requests) == 1


async def test_status_reads_do_retry_transient_failures_as_the_control():
    recorder = Recorder(httpx.Response(503), httpx.Response(200, json=AVATAR_FIXTURE))
    read = await _gateway(recorder).get_avatar("ai4ia-0123456789abcdef0123")
    assert read.kind == "found" and read.avatar.state == "succeeded"
    assert len(recorder.requests) == 2
    assert all(request.method == "GET" and "S7PTTL" not in request.headers for request in recorder.requests)


async def test_reads_classify_absent_unknown_and_never_follow_redirects():
    absent = await _gateway(Recorder(httpx.Response(404, json=NOT_FOUND))).get_avatar(
        "ai4ia-0123456789abcdef0123"
    )
    assert absent.kind == "absent"
    moved = Recorder(httpx.Response(302, headers={"Location": "https://attacker.example/x"}))
    redirected = await _gateway(moved).get_avatar("ai4ia-0123456789abcdef0123")
    assert redirected.kind == "unknown" and len(moved.requests) == 1


@pytest.mark.parametrize("reply, expected", [
    (httpx.Response(204), "deleted"), (httpx.Response(200), "deleted"),
    (httpx.Response(404, json=NOT_FOUND), "absent"), (httpx.Response(409), "rejected"),
    (httpx.Response(503), "failed"), (httpx.ReadTimeout("slow"), "failed"),
])
async def test_delete_counts_404_as_gone_and_is_single_attempt(reply, expected):
    recorder = Recorder(reply)
    assert await _gateway(recorder).delete_avatar("ai4ia-0123456789abcdef0123") == expected
    assert len(recorder.requests) == 1 and recorder.requests[0].method == "DELETE"


async def test_features_and_project_reads():
    assert await _gateway(Recorder(httpx.Response(200, json=["CustomAvatar"]))).features() == frozenset(
        {"CustomAvatar"}
    )
    assert await _gateway(Recorder(httpx.Response(200, json={"CustomAvatar": True}))).features() is None
    assert await _gateway(Recorder(httpx.Response(500))).features() is None
    assert await _gateway(Recorder(httpx.Response(200, json={"kind": "PhotoAvatar"}))).get_project() == "present"
    assert await _gateway(Recorder(httpx.Response(404, json=NOT_FOUND))).get_project() == "absent"
    assert await _gateway(Recorder(httpx.Response(401))).get_project() == "unknown"
    created = Recorder(httpx.Response(201))
    assert await _gateway(created).create_project() is True
    assert created.requests[0].method == "PUT" and created.requests[0].content == b""
    assert await _gateway(Recorder(httpx.Response(409))).create_project() is True
    assert await _gateway(Recorder(httpx.Response(500))).create_project() is False


async def test_bearer_mode_and_priority_band_follow_the_model_gateway_headers():
    from ai4ia_api.gateway.priority import PRIORITY_HEADER, set_request_priority

    recorder = Recorder(httpx.Response(200, json=["CustomAvatar"]))
    set_request_priority(1)
    try:
        await _gateway(recorder, model_gateway_auth_mode="bearer").features()
    finally:
        set_request_priority(None)
    request = recorder.requests[0]
    assert request.headers["authorization"] == "Bearer proxy-ingress-key"
    assert request.headers[PRIORITY_HEADER] == "1"


# ---- a 404 proves absence only in the provider's own shape ----------------

APIM_NOT_FOUND = {"statusCode": 404, "message": "Resource not found"}
NOT_PROVIDER_404_BODIES = [
    pytest.param(APIM_NOT_FOUND, id="apim-missing-api"),
    pytest.param(None, id="empty-body"),
    pytest.param({"code": "NotFound"}, id="top-level-code"),
    pytest.param({"error": {"code": "ResourceNotFound"}}, id="other-nested-code"),
    pytest.param({"error": "NotFound"}, id="error-not-an-object"),
    pytest.param("Resource not found", id="text-body"),
]


def _reply_404(body) -> httpx.Response:
    if body is None:
        return httpx.Response(404)
    if isinstance(body, str):
        return httpx.Response(404, text=body)
    return httpx.Response(404, json=body)


@pytest.mark.parametrize("body", NOT_PROVIDER_404_BODIES)
async def test_only_the_providers_not_found_proves_an_avatar_or_project_absent(body):
    """APIM answers 404 for a missing or rolled-back API; that proves nothing."""
    other = await _gateway(Recorder(_reply_404(body))).get_avatar("ai4ia-0123456789abcdef0123")
    assert (other.kind, other.status) == ("unknown", 404)
    assert await _gateway(Recorder(_reply_404(body))).delete_avatar("ai4ia-0123456789abcdef0123") == "failed"
    assert await _gateway(Recorder(_reply_404(body))).get_project() == "unknown"
    # Control: the identical calls, answered in the provider's own shape.
    absent = await _gateway(Recorder(httpx.Response(404, json=NOT_FOUND))).get_avatar(
        "ai4ia-0123456789abcdef0123",
    )
    assert absent.kind == "absent"
    assert await _gateway(Recorder(httpx.Response(404, json=NOT_FOUND))).delete_avatar(
        "ai4ia-0123456789abcdef0123",
    ) == "absent"
    assert await _gateway(Recorder(httpx.Response(404, json=NOT_FOUND))).get_project() == "absent"


def test_the_generated_policy_cannot_answer_in_the_providers_not_found_shape():
    policy = ET.parse(REPO / "infra" / "policies" / "photo-avatars.xml").getroot()
    refusals = policy.findall(".//return-response")
    assert len(refusals) >= 2  # the 400 request refusal and the 502 gateway error
    for refusal in refusals:
        status = int(refusal.find("set-status").get("code"))
        body = json.loads(refusal.find("set-body").text)
        assert status != 404
        assert body["error"]["code"] != provider_module.PROVIDER_NOT_FOUND
        assert not provider_not_found(httpx.Response(404, json=body))
    # Control: the provider's own shape is recognized.
    assert provider_not_found(httpx.Response(404, json=NOT_FOUND))


# ---- a sent create is never lost to a parsing or classification fault ----

DEEP = b"[" * 200_000 + b"]" * 200_000


async def test_a_pathologically_nested_body_is_read_as_no_body_not_raised():
    with pytest.raises(RecursionError):
        json.loads(DEEP)  # the fixture really does break the parser
    created = await _gateway(Recorder(httpx.Response(201, content=DEEP))).create_avatar(
        "ai4ia-0123456789abcdef0123", build_create_body("A host.", {}),
    )
    # The status proves acceptance; the unreadable body only loses the state.
    assert created.kind == "accepted" and created.status == 201 and created.avatar.state == "pending"
    read = await _gateway(Recorder(httpx.Response(200, content=DEEP))).get_avatar(
        "ai4ia-0123456789abcdef0123",
    )
    assert read.kind == "found" and read.avatar.state == "pending"
    assert await _gateway(Recorder(httpx.Response(404, content=DEEP))).get_project() == "unknown"


async def test_any_classification_fault_leaves_a_sent_create_unknown(monkeypatch):
    def broken(_body):
        raise RuntimeError("unexpected provider shape")

    control = await _gateway(Recorder(httpx.Response(201, json=AVATAR_FIXTURE))).create_avatar(
        "ai4ia-0123456789abcdef0123", build_create_body("A host.", {}),
    )
    assert control.kind == "accepted"
    monkeypatch.setattr(provider_module, "parse_avatar", broken)
    recorder = Recorder(httpx.Response(201, json=AVATAR_FIXTURE))
    outcome = await _gateway(recorder).create_avatar(
        "ai4ia-0123456789abcdef0123", build_create_body("A host.", {}),
    )
    assert (outcome.kind, outcome.status) == ("unknown", 201)
    assert len(recorder.requests) == 1  # classified, never re-sent


# ---- merge seam: the proxy's App Configuration timeout floor --------------

def test_every_proxied_photo_avatar_wait_fits_under_the_proxy_timeout_floor():
    """#529 bounds App Configuration's request timeout below by MinTimeoutMs.

    Its contract enumerates the API's gateway_*_timeout_seconds settings; these
    adapter constants are proxied waits too, so the floor must cover them.
    """
    source = (REPO / "proxy" / "SimpleL7Proxy" / "Config" / "AppConfigKeyPolicy.cs").read_text(
        encoding="utf-8",
    )
    match = re.search(r"internal const int MinTimeoutMs = ([\d_]+);", source)
    assert match is not None
    floor_ms = int(match[1].replace("_", ""))
    waits = {
        "create": CREATE_TIMEOUT_SECONDS, "read": READ_TIMEOUT_SECONDS,
        "features": FEATURES_TIMEOUT_SECONDS,
    }
    assert 1000 * max(waits.values()) <= floor_ms, waits
    # The create's own TTL ends the proxy's wait before the API's, so the API
    # always hears an answer rather than timing out on a send it cannot see.
    assert CREATE_PROXY_TTL_SECONDS < CREATE_TIMEOUT_SECONDS