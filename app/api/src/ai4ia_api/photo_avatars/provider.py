"""The one module that knows the undocumented photo avatar creation contract.

The creation surface is the Foundry portal's own endpoint (``/CustomAvatar/...``,
``2023-12-01-preview``); it is not in the public REST reference and can change
without notice. Everything provider-shaped that FastAPI needs lives here:

* AI4IA's front paths on the governed route (FastAPI -> SimpleL7Proxy ->
  ``ai4ia-photo-avatars-v1`` on APIM). APIM maps them to the provider path and
  pins the api-version, both rendered from the catalog, so FastAPI never learns
  the home account or project name;
* provider avatar id generation and validation;
* the create body, response parsing, bounded error codes and the state machine
  (an unrecognized state is pending, never success);
* the transport: one admitted, single-attempt create; GET reads with the
  shared transient retry; a single-attempt delete.

Observed contract (read-only, 2026-09-26): ``GET .../photoavatars/{id}`` returns
``{id, projectId, state, createdAt, lastUpdatedAt, supportedModels,
promptImageUri, properties{prompt, gender, age, ethnicity, style}, description}``;
``state`` reached ``Succeeded``; an unknown id is ``404 {"error":{"code":"NotFound"}}``;
the features read returns a JSON array of strings. Avatar ids are scoped to the
account, not the avatar project, which is why APIM accepts only AI4IA-issued ids.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from ..config import GatewayAuthMode, Settings
from ..gateway.priority import PRIORITY_HEADER, get_request_priority
from ..hard_quota.dispatch import admitted_dispatch
from ..http_retry import request_with_retry
from .catalog import ATTRIBUTE_NAMES

logger = logging.getLogger(__name__)

API_PATH = "ai4ia-photo-avatars-v1"
PROVIDER_ID_PREFIX = "ai4ia-"
# AI4IA-issued ids: 26 characters, under the receipt redactor's 32-character
# threshold, and inside the provider's own rule below. APIM enforces the same
# stricter pattern (see scripts/gen-voice-provider-catalog.py).
PROVIDER_ID_PATTERN = re.compile(r"^ai4ia-[0-9a-f]{20}$")
PROVIDER_ID_RULE = re.compile(r"^[A-Za-z][\w.-]{1,62}[\dA-Za-z]$")
_ERROR_CODE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_STATE_TEXT = re.compile(r"^[A-Za-z]{1,40}$")

# A create answered with one of these was not accepted, so nothing was billed.
# 408, 412 (the proxy's attempt/TTL refusal), every 5xx and any transport
# failure after the request may have left leave acceptance unknown.
DEFINITE_REJECTIONS = frozenset({400, 401, 403, 404, 409, 413, 415, 422, 429})

CREATE_TIMEOUT_SECONDS = 50.0
# SimpleL7Proxy never sends a queued create after this many seconds, which
# bounds how late an unknown create can still be accepted.
CREATE_PROXY_TTL_SECONDS = 45
READ_TIMEOUT_SECONDS = 15.0
FEATURES_TIMEOUT_SECONDS = 5.0

ProviderState = Literal["pending", "succeeded", "failed"]


def new_provider_avatar_id() -> str:
    return PROVIDER_ID_PREFIX + secrets.token_hex(10)


def valid_provider_avatar_id(value: str) -> bool:
    return bool(PROVIDER_ID_PATTERN.fullmatch(value)) and bool(PROVIDER_ID_RULE.fullmatch(value))


def classify_state(raw: Any) -> ProviderState:
    """Map a provider ``state`` to AI4IA's view. Only an exact success is success."""
    text = raw.strip().lower() if isinstance(raw, str) else ""
    if text == "succeeded":
        return "succeeded"
    if text == "failed":
        return "failed"
    # NotStarted, Running and anything unrecognized: keep reconciling.
    return "pending"


class PreviewLink:
    """The provider-issued preview SAS link. It never prints, logs or serializes."""

    __slots__ = ("_url",)

    def __init__(self, url: str) -> None:
        self._url = url

    def reveal(self) -> str:
        """For the bounded preview fetch only."""
        return self._url

    def __repr__(self) -> str:
        return "PreviewLink(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("A preview link is not serializable.")


@dataclass(frozen=True)
class ProviderAvatar:
    state: ProviderState
    raw_state: str | None
    preview: PreviewLink | None
    error_code: str | None


def bounded_error_code(body: Any) -> str | None:
    """The provider-owned error code only; messages can echo the prompt."""
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else body.get("code")
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else None


def parse_avatar(body: Any) -> ProviderAvatar:
    if not isinstance(body, dict):
        return ProviderAvatar("pending", None, None, None)
    raw_state = body.get("state")
    uri = body.get("promptImageUri")
    return ProviderAvatar(
        state=classify_state(raw_state),
        raw_state=raw_state if isinstance(raw_state, str) and _STATE_TEXT.fullmatch(raw_state) else None,
        preview=PreviewLink(uri) if isinstance(uri, str) and uri else None,
        error_code=bounded_error_code(body),
    )


def parse_features(body: Any) -> frozenset[str] | None:
    """The account's custom avatar features, or None for any unexpected shape."""
    if not isinstance(body, list) or not all(isinstance(item, str) for item in body):
        return None
    return frozenset(body)


def build_create_body(prompt: str, attributes: dict[str, str | None]) -> dict[str, Any]:
    properties: dict[str, str] = {"prompt": prompt}
    for name in ATTRIBUTE_NAMES:
        value = attributes.get(name)
        if value is not None:
            properties[name] = value
    # No ``description``: the display name stays in AI4IA and APIM refuses any
    # other key, including the fields of the photo-upload creation path.
    return {"properties": properties}


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, RecursionError):
        # An unreadable or pathologically nested body is no body at all.
        return None


PROVIDER_NOT_FOUND = "NotFound"


def provider_not_found(response: httpx.Response) -> bool:
    """A 404 proves absence only in the provider's own error shape.

    APIM answers 404 for a missing or rolled-back API as
    ``{"statusCode":404,...}``, and the generated policy's own refusals use
    distinct codes, so neither can pass for the provider's
    ``{"error":{"code":"NotFound"}}``. Such a 404 says nothing about the
    avatar and stays unknown.
    """
    if response.status_code != 404:
        return False
    body = _json(response)
    error = body.get("error") if isinstance(body, dict) else None
    return isinstance(error, dict) and error.get("code") == PROVIDER_NOT_FOUND


def _classify_create(response: httpx.Response) -> CreateOutcome:
    status = response.status_code
    body_json = _json(response)
    if 200 <= status < 300:
        # The status proves acceptance; the body only adds state and a preview.
        return CreateOutcome("accepted", status, parse_avatar(body_json))
    if status in DEFINITE_REJECTIONS:
        return CreateOutcome("rejected", status, error_code=bounded_error_code(body_json))
    return CreateOutcome("unknown", status, error_code=bounded_error_code(body_json))


CreateKind = Literal["accepted", "rejected", "not_sent", "unknown"]


@dataclass(frozen=True)
class CreateOutcome:
    kind: CreateKind
    status: int | None = None
    avatar: ProviderAvatar | None = None
    error_code: str | None = None


ReadKind = Literal["found", "absent", "unknown"]


@dataclass(frozen=True)
class ReadOutcome:
    kind: ReadKind
    avatar: ProviderAvatar | None = None
    status: int | None = None


DeleteKind = Literal["deleted", "absent", "rejected", "failed"]
ProjectState = Literal["present", "absent", "unknown"]


class PhotoAvatarGateway:
    """Transport to the governed photo avatar route.

    Authenticates to SimpleL7Proxy with the API's existing proxy-ingress
    credential; the proxy's dedicated host injects the API-scoped APIM key, and
    APIM authenticates to the home account with its managed identity.
    """

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        base = httpx.URL(settings.model_gateway_url)
        self._root = str(base.copy_with(path=f"/{API_PATH}", query=None, fragment=None))
        self._auth_mode = settings.model_gateway_auth_mode
        self._api_key = settings.model_gateway_api_key
        self._api_key_header = settings.model_gateway_api_key_header
        self._retry_policy = settings.outbound_retry_policy()
        self._hard_quota_enabled = settings.hard_quota_enabled
        self._group_policy_enabled = settings.group_policy_enabled
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False),
            follow_redirects=False,
            trust_env=False,
            timeout=READ_TIMEOUT_SECONDS,
        )

    @property
    def root(self) -> str:
        return self._root

    def url(self, route: str) -> str:
        return f"{self._root}{route}"

    def avatar_url(self, provider_id: str) -> str:
        if not valid_provider_avatar_id(provider_id):
            raise ValueError("Not an AI4IA-issued provider avatar id.")
        return self.url(f"/photoavatars/{provider_id}")

    def _headers(
        self, correlation_id: str | None, *, json_body: bool = False, ttl: int | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if json_body:
            headers["Content-Type"] = "application/json"
        if correlation_id:
            headers["x-correlation-id"] = correlation_id
        if self._auth_mode == GatewayAuthMode.api_key and self._api_key:
            headers[self._api_key_header] = self._api_key
        elif self._auth_mode == GatewayAuthMode.bearer and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        band = get_request_priority()
        if band is not None:
            headers[PRIORITY_HEADER] = str(band)
        if ttl is not None:
            headers["S7PTTL"] = str(ttl)
        return headers

    async def _get(self, route: str, correlation_id: str | None, timeout: float) -> httpx.Response:
        return await request_with_retry(
            lambda: self._client.get(
                self.url(route), headers=self._headers(correlation_id), timeout=timeout,
            ),
            method="GET",
            policy=self._retry_policy,
        )

    async def features(self, *, correlation_id: str | None = None) -> frozenset[str] | None:
        """The account's features, or None when the read failed or looked wrong."""
        try:
            response = await self._get("/features", correlation_id, FEATURES_TIMEOUT_SECONDS)
        except (httpx.HTTPError, OSError):
            return None
        if response.status_code != 200:
            return None
        return parse_features(_json(response))

    async def get_project(self, *, correlation_id: str | None = None) -> ProjectState:
        try:
            response = await self._get("/project", correlation_id, READ_TIMEOUT_SECONDS)
        except (httpx.HTTPError, OSError):
            return "unknown"
        if response.status_code == 200:
            return "present"
        if provider_not_found(response):
            return "absent"
        return "unknown"

    async def create_project(self, *, correlation_id: str | None = None) -> bool:
        """Create the avatar project. APIM owns the body; FastAPI sends none.

        The only unmetered provider write: it creates a free container for
        avatars, never an avatar. Callers run it only after ``get_project``
        reported the project absent, so an existing project is never replaced.
        """
        try:
            response = await self._client.put(
                self.url("/project"), headers=self._headers(correlation_id), content=b"",
                timeout=READ_TIMEOUT_SECONDS,
            )
        except (httpx.HTTPError, OSError):
            return False
        return response.status_code in {200, 201, 409}

    async def create_avatar(
        self, provider_id: str, body: dict[str, Any], *, correlation_id: str | None = None,
    ) -> CreateOutcome:
        """Send one create through owner admission. Never retried.

        Refusals raised before the request leaves (hard-quota or policy
        admission) propagate so the caller can release its reservation. Once the
        request may have been sent, every outcome is classified instead: an
        accepted create is billable, and an unknown one is reconciled with a
        status read, never repeated.
        """
        url = self.avatar_url(provider_id)
        payload = {
            "operation": "photo_avatar.create",
            "avatar": provider_id[:12],
            "bodyDigest": hashlib.sha256(repr(sorted(body["properties"].items())).encode()).hexdigest(),
        }
        state: dict[str, Any] = {"sent": False, "response": None}
        try:
            async with admitted_dispatch(
                "avatar", payload, target=url,
                required=self._hard_quota_enabled, policy_required=self._group_policy_enabled,
            ) as admission:
                state["sent"] = True
                response = await self._client.put(
                    url,
                    headers=self._headers(correlation_id, json_body=True, ttl=CREATE_PROXY_TTL_SECONDS),
                    json=body,
                    timeout=CREATE_TIMEOUT_SECONDS,
                )
                state["response"] = response
                if response.is_success:
                    admission.report()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # The proxy was never reached, so nothing was sent anywhere.
            return CreateOutcome("not_sent")
        except (httpx.HTTPError, OSError, TimeoutError):
            return CreateOutcome("unknown")
        except Exception:
            if not state["sent"]:
                raise
            if state["response"] is None:
                return CreateOutcome("unknown")
            # The provider answered but admission settlement failed afterwards;
            # classify the answer rather than losing billable work.
            logger.warning("photo avatar create settlement failed after the provider answered")
        response = state["response"]
        if response is None:
            return CreateOutcome("unknown")
        try:
            return _classify_create(response)
        except Exception:  # noqa: BLE001 - a sent create is never lost to a classification fault
            logger.warning(
                "photo avatar create response could not be classified status=%s", response.status_code,
            )
            return CreateOutcome("unknown", response.status_code)

    async def get_avatar(self, provider_id: str, *, correlation_id: str | None = None) -> ReadOutcome:
        url_route = self.avatar_url(provider_id).removeprefix(self._root)
        try:
            response = await self._get(url_route, correlation_id, READ_TIMEOUT_SECONDS)
        except (httpx.HTTPError, OSError):
            return ReadOutcome("unknown")
        if response.status_code == 200:
            return ReadOutcome("found", parse_avatar(_json(response)), 200)
        if provider_not_found(response):
            return ReadOutcome("absent", status=404)
        return ReadOutcome("unknown", status=response.status_code)

    async def delete_avatar(self, provider_id: str, *, correlation_id: str | None = None) -> DeleteKind:
        url = self.avatar_url(provider_id)
        try:
            response = await self._client.delete(
                url, headers=self._headers(correlation_id), timeout=READ_TIMEOUT_SECONDS,
            )
        except (httpx.HTTPError, OSError):
            return "failed"
        if response.status_code in {200, 202, 204}:
            return "deleted"
        if provider_not_found(response):
            return "absent"
        if response.status_code in {400, 409}:
            return "rejected"
        return "failed"

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
