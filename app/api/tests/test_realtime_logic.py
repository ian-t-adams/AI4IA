"""Voice Live (Phase 10) pure-logic unit tests.

Covers the IO-free helpers in ``routers/realtime.py`` that make the relay
governable: subprotocol credential parsing, the Origin allowlist decision,
realtime deployment resolution, upstream URL/header construction, and the
disabled-by-default config posture. No network, no WebSocket — just functions.
"""
from __future__ import annotations

import asyncio

import pytest

from ai4ia_api.auth.base import AuthCredentials, AuthError, AuthenticatedUser
from ai4ia_api.catalog import DeploymentOption, ModelCatalog, ModelEntry
from ai4ia_api.config import GatewayAuthMode
from ai4ia_api.routers.realtime import (
    BEARER_SUBPROTOCOL,
    DEV_SUBPROTOCOL,
    AuthSubprotocol,
    RealtimeResolutionError,
    authenticate_subprotocol,
    build_upstream_headers,
    build_upstream_url,
    origin_allowed,
    parse_auth_subprotocols,
    resolve_realtime_deployment,
)
from tests.conftest import make_settings


def _opt(region: str, name: str) -> DeploymentOption:
    return DeploymentOption(region=region, sku="GlobalStandard", deploymentName=name)


def _catalog() -> ModelCatalog:
    return ModelCatalog(
        models=[
            ModelEntry(
                id="gpt-5.2",
                displayName="GPT-5.2",
                category="chat",
                format="OpenAI",
                options=[_opt("eastus2", "gpt-5.2-eastus2")],
            ),
            ModelEntry(
                id="gpt-realtime",
                displayName="GPT Realtime",
                category="realtime",
                format="OpenAI",
                options=[
                    _opt("eastus2", "gpt-realtime-eastus2"),
                    _opt("swedencentral", "gpt-realtime-swedencentral"),
                ],
            ),
            ModelEntry(
                id="gpt-realtime-mini",
                displayName="GPT Realtime Mini",
                category="realtime",
                format="OpenAI",
                options=[_opt("eastus2", "gpt-realtime-mini-eastus2")],
            ),
        ]
    )


# --------------------------------------------------------------------------- #
# parse_auth_subprotocols
# --------------------------------------------------------------------------- #


def test_parse_bearer_subprotocol():
    parsed = parse_auth_subprotocols([BEARER_SUBPROTOCOL, "the.access.token"])
    assert parsed == AuthSubprotocol(marker=BEARER_SUBPROTOCOL, credential="the.access.token")


def test_parse_dev_subprotocol():
    parsed = parse_auth_subprotocols([DEV_SUBPROTOCOL, "alice"])
    assert parsed == AuthSubprotocol(marker=DEV_SUBPROTOCOL, credential="alice")


def test_parse_extra_subprotocols_ignored():
    parsed = parse_auth_subprotocols([BEARER_SUBPROTOCOL, "tok", "something-else"])
    assert parsed is not None
    assert parsed.credential == "tok"


@pytest.mark.parametrize(
    "offered",
    [
        [],
        [BEARER_SUBPROTOCOL],  # marker without credential
        ["unknown-marker", "tok"],  # unrecognized marker
        [BEARER_SUBPROTOCOL, "   "],  # blank credential
        [DEV_SUBPROTOCOL, ""],  # empty credential
    ],
)
def test_parse_rejects_malformed(offered):
    assert parse_auth_subprotocols(offered) is None


# --------------------------------------------------------------------------- #
# origin_allowed
# --------------------------------------------------------------------------- #


def test_origin_allowlist_exact_match():
    allowed = ["https://app.example.com"]
    assert origin_allowed("https://app.example.com", allowed, reflect_when_unset=True)


def test_origin_allowlist_mismatch_rejected():
    allowed = ["https://app.example.com"]
    assert not origin_allowed("https://evil.example.com", allowed, reflect_when_unset=True)


def test_origin_missing_rejected_when_allowlist_set():
    allowed = ["https://app.example.com"]
    assert not origin_allowed(None, allowed, reflect_when_unset=True)


def test_origin_empty_allowlist_reflects_in_dev():
    assert origin_allowed("https://anything", [], reflect_when_unset=True)
    assert origin_allowed(None, [], reflect_when_unset=True)


def test_origin_empty_allowlist_fail_closed_in_prod():
    # Deployed env with no configured allowlist must reject everything.
    assert not origin_allowed("https://anything", [], reflect_when_unset=False)
    assert not origin_allowed(None, [], reflect_when_unset=False)


# --------------------------------------------------------------------------- #
# resolve_realtime_deployment
# --------------------------------------------------------------------------- #


def test_resolve_defaults_to_first_realtime_model():
    model_id, deployment = resolve_realtime_deployment(_catalog(), None, None)
    assert model_id == "gpt-realtime"
    assert deployment.deploymentName == "gpt-realtime-eastus2"


def test_resolve_explicit_realtime_model():
    model_id, deployment = resolve_realtime_deployment(_catalog(), "gpt-realtime-mini", None)
    assert model_id == "gpt-realtime-mini"
    assert deployment.deploymentName == "gpt-realtime-mini-eastus2"


def test_resolve_honors_region():
    _, deployment = resolve_realtime_deployment(_catalog(), "gpt-realtime", "swedencentral")
    assert deployment.region == "swedencentral"
    assert deployment.deploymentName == "gpt-realtime-swedencentral"


def test_resolve_rejects_non_realtime_model():
    with pytest.raises(RealtimeResolutionError):
        resolve_realtime_deployment(_catalog(), "gpt-5.2", None)


def test_resolve_rejects_unknown_model():
    with pytest.raises(RealtimeResolutionError):
        resolve_realtime_deployment(_catalog(), "no-such-model", None)


def test_resolve_no_realtime_models_available():
    chat_only = ModelCatalog(
        models=[
            ModelEntry(
                id="gpt-5.2",
                displayName="GPT-5.2",
                category="chat",
                format="OpenAI",
                options=[_opt("eastus2", "gpt-5.2-eastus2")],
            )
        ]
    )
    with pytest.raises(RealtimeResolutionError):
        resolve_realtime_deployment(chat_only, None, None)


# --------------------------------------------------------------------------- #
# build_upstream_url
# --------------------------------------------------------------------------- #


def test_build_url_https_to_wss():
    url = build_upstream_url("https://apim.example.com/openai", "2025-04-01-preview", "dep-1")
    assert url == (
        "wss://apim.example.com/openai/realtime"
        "?api-version=2025-04-01-preview&deployment=dep-1"
    )


def test_build_url_http_to_ws():
    url = build_upstream_url("http://gateway.test/openai", "2025-04-01-preview", "dep-1")
    assert url.startswith("ws://gateway.test/openai/realtime")


def test_build_url_strips_trailing_slash():
    url = build_upstream_url("https://apim.example.com/openai/", "v1", "dep-1")
    assert "/openai/realtime" in url
    assert "/openai//realtime" not in url


def test_build_url_encodes_deployment_and_version():
    url = build_upstream_url("https://h/openai", "2025-04-01-preview", "dep name/special")
    assert "deployment=dep%20name%2Fspecial" in url


# --------------------------------------------------------------------------- #
# build_upstream_headers
# --------------------------------------------------------------------------- #


def test_headers_api_key_mode():
    headers = build_upstream_headers(GatewayAuthMode.api_key, "secret-key", "corr-1")
    assert headers["Ocp-Apim-Subscription-Key"] == "secret-key"
    assert "Authorization" not in headers
    assert headers["x-correlation-id"] == "corr-1"


def test_headers_bearer_mode():
    headers = build_upstream_headers(GatewayAuthMode.bearer, "the-token", None)
    assert headers["Authorization"] == "Bearer the-token"
    assert "Ocp-Apim-Subscription-Key" not in headers
    assert "x-correlation-id" not in headers


def test_headers_none_mode_has_no_credential():
    headers = build_upstream_headers(GatewayAuthMode.none, None, "corr-2")
    assert "Authorization" not in headers
    assert "Ocp-Apim-Subscription-Key" not in headers
    assert headers["x-correlation-id"] == "corr-2"


def test_headers_api_key_mode_without_key_omits_header():
    headers = build_upstream_headers(GatewayAuthMode.api_key, None, None)
    assert headers == {}


# --------------------------------------------------------------------------- #
# authenticate_subprotocol (provider dispatch + dev-permission gate)
# --------------------------------------------------------------------------- #


class _DummyProvider:
    """Echoes the dev override or the bearer token into the user's subject."""

    async def authenticate(self, credentials: AuthCredentials) -> AuthenticatedUser:
        subject = credentials.header("X-Dev-User") or (credentials.token or "")
        return AuthenticatedUser(
            internal_user_id=f"id::{subject}",
            subject=subject,
            issuer="dummy",
            provider="dummy",
        )


def test_authenticate_dev_subprotocol_when_permitted():
    settings = make_settings(env="local")
    user = asyncio.run(
        authenticate_subprotocol(
            _DummyProvider(), settings, AuthSubprotocol(DEV_SUBPROTOCOL, "alice")
        )
    )
    assert user.subject == "alice"


def test_authenticate_dev_subprotocol_denied_when_not_permitted():
    settings = make_settings(env="dev", allow_dev_auth=False)
    with pytest.raises(AuthError):
        asyncio.run(
            authenticate_subprotocol(
                _DummyProvider(), settings, AuthSubprotocol(DEV_SUBPROTOCOL, "alice")
            )
        )


def test_authenticate_bearer_passes_token_to_provider():
    settings = make_settings(env="local")
    user = asyncio.run(
        authenticate_subprotocol(
            _DummyProvider(), settings, AuthSubprotocol(BEARER_SUBPROTOCOL, "tok-xyz")
        )
    )
    assert user.subject == "tok-xyz"
