"""Default-absent, reduction-only binding for the governed one-attempt transport.

There is deliberately no deployed capability verifier here or in the app factory.
The verifier is a trusted server integration, not an environment switch, DTO,
response header, or a claim supplied by a model. See docs/hard-quota-admission.md.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..hard_quota.coverage import AttemptEnvelope, model_for_deployment, supported_attempt_payload
from ..hard_quota.models import QuotaError, Surface

ATTEMPT_VERSION = "ai4ia-one-attempt-v1"
ATTEMPT_API_NAME = "ai4ia-attempts-v1"
ATTEMPT_PATH = f"/{ATTEMPT_API_NAME}/openai"
ATTEMPT_OPERATIONS = (
    ("POST", "/openai/responses"),
    ("POST", "/openai/deployments/{deployment}/chat/completions"),
    ("POST", "/openai/deployments/{deployment}/embeddings"),
)
ATTEMPT_HEADER = "x-ai4ia-attempt"
PROXY_PROOF_HEADER = "x-ai4ia-proxy-attempt"
ACK_HEADER = "x-ai4ia-attempt-ack"
INTERNAL_HEADERS = frozenset({ATTEMPT_HEADER, PROXY_PROOF_HEADER, ACK_HEADER})
MAX_BODY_BYTES = 1024 * 1024


@dataclass(frozen=True)
class GatewayRouteBinding:
    """Required deployment readback, not authority inferred from configuration.

    The verifier must establish API-scoped key membership, effective policy,
    complete serving revisions and a transition-fenced evidence epoch. Neither
    these structural checks nor a response ACK establish that evidence.
    """

    apim_url: str
    api_resource_id: str
    api_revision: str
    subscription_resource_id: str
    subscription_scope: str
    operations: tuple[tuple[str, str], ...]
    evidence_epoch: str

    def validate(self) -> None:
        url = httpx.URL(self.apim_url)
        api_match = re.fullmatch(
            r"(/subscriptions/[0-9a-f-]{36}/resourceGroups/[A-Za-z0-9_.()-]+"
            r"/providers/Microsoft\.ApiManagement/service/[A-Za-z0-9-]+)/apis/"
            + ATTEMPT_API_NAME, self.api_resource_id,
        )
        if (
            url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
            or url.path != "/" or self.apim_url != str(url).rstrip("/")
            or api_match is None or self.subscription_scope != self.api_resource_id
            or re.fullmatch(r"[1-9][0-9]{0,8}", self.api_revision) is None
            or self.operations != ATTEMPT_OPERATIONS
            or re.fullmatch(r"[0-9a-f]{64}", self.evidence_epoch) is None
        ):
            raise QuotaError("Verified versioned gateway route is unavailable.")
        subscription_prefix = api_match[1] + "/subscriptions/"
        if (
            not self.subscription_resource_id.startswith(subscription_prefix)
            or re.fullmatch(
                r"[A-Za-z0-9-]+-proxy-attempts-v1",
                self.subscription_resource_id.removeprefix(subscription_prefix),
            ) is None
        ):
            raise QuotaError("Verified versioned gateway subscription is unavailable.")


@dataclass(frozen=True)
class VerifiedGatewayCapability:
    """Output of an independently trusted, exact-deployment compatibility check.

    Well-formed hashes are NOT verification. Only an injected verifier may attest
    their deployed meaning, freshness, complete replica coverage, transport and
    effective-policy topology. No shipping factory constructs this object.
    """

    gateway_url: str
    proxy_image: str
    apim_policy_sha256: str
    topology_sha256: str
    catalog_sha256: str
    expires_at: float
    route: GatewayRouteBinding
    api_image: str
    version: str = ATTEMPT_VERSION

    def validate(self, gateway_url: str) -> None:
        self.route.validate()
        url = httpx.URL(self.gateway_url)
        if (
            self.version != ATTEMPT_VERSION
            or self.gateway_url != gateway_url
            or url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
            or url.path != "/openai" or str(url) != self.gateway_url
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.proxy_image) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.api_image) is None
            or any(re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in (
                self.apim_policy_sha256, self.topology_sha256, self.catalog_sha256,
            ))
            or not time.time() < self.expires_at <= time.time() + 300
        ):
            raise QuotaError("Verified gateway attempt capability is unavailable.")

    @property
    def envelope(self) -> AttemptEnvelope:
        return AttemptEnvelope(version=self.version, max_attempts=1)


class GatewayCapabilityVerifier(Protocol):
    @property
    def capability(self) -> VerifiedGatewayCapability | None: ...

    async def verify(self, capability: VerifiedGatewayCapability) -> None:
        """Verify the typed route/readback and transition-fenced epoch before admission.

        Issuance requires all serving API/proxy images, the complete effective
        API revision/policies and operation inventory, API-only subscription
        membership, non-replaying ingress and provider meter compatibility.
        Invalidate the epoch BEFORE any route/key/policy/replica transition.
        No implementation or evidence authority ships with this contract.
        """
        ...


_selected: ContextVar[str | None] = ContextVar("gateway_no_replay_owner", default=None)
_prepared: ContextVar[PreparedAttempt | None] = ContextVar("gateway_attempt", default=None)


@contextmanager
def no_replay_scope(owner: str) -> Iterator[None]:
    """A trusted caller can require less egress, never grant any permission."""
    previous = _selected.get()
    if not owner or (previous is not None and previous != owner):
        raise QuotaError("Gateway attempt owner does not match.", code=403)
    token = _selected.set(owner)
    try:
        yield
    finally:
        _selected.reset(token)


def no_replay_selected() -> bool:
    return _selected.get() is not None


def versioned_target(
    gateway_url: str, target: str, *, surface: Surface, deployment: str, api: str,
) -> str:
    """Select only a fixed operation; never translate an unknown/failed route."""
    base, url = httpx.URL(gateway_url), httpx.URL(target)
    relative = url.path.removeprefix("/openai")
    expected = (
        f"/deployments/{deployment}/embeddings" if surface == "embedding"
        else "/responses" if api == "responses"
        else f"/deployments/{deployment}/chat/completions" if api == "chat"
        else None
    )
    if (
        surface not in {"chat", "embedding"} or expected is None or relative != expected
        or base.path != "/openai" or base.scheme != "https" or not base.host
        or base.userinfo or base.query or base.fragment
        or url.scheme != base.scheme or url.host != base.host or url.port != base.port
        or url.userinfo or url.fragment or str(url) != target
        or url.raw_path.split(b"?", 1)[0] != url.path.encode("ascii", errors="replace")
        or re.fullmatch(r"[A-Za-z0-9_.-]+", deployment) is None
        or (url.query and re.fullmatch(rb"api-version=[A-Za-z0-9_.-]{1,64}", url.query) is None)
    ):
        raise QuotaError("Unsupported versioned gateway operation.")
    return str(url.copy_with(path=ATTEMPT_PATH + relative))


def _body(payload: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
    except (ValueError, TypeError, RecursionError) as exc:
        raise QuotaError("Unsupported bounded gateway payload.") from exc
    if len(encoded) > MAX_BODY_BYTES:
        raise QuotaError("Unsupported bounded gateway payload.")
    return encoded


@dataclass
class PreparedAttempt:
    owner: str
    surface: Surface
    deployment: str
    target: str
    body: bytes
    capability: VerifiedGatewayCapability
    verifier: GatewayCapabilityVerifier
    gateway_url: str
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)
    claimed: bool = False
    active: bool = True
    binding: Token[PreparedAttempt | None] | None = field(default=None, repr=False)

    def unbind(self) -> None:
        if self.binding is not None:
            _prepared.reset(self.binding)
            self.binding = None

    def match(
        self, surface: Surface, payload: dict[str, Any], *, deployment: str | None,
        target: str | None, owner: str,
    ) -> AttemptEnvelope:
        self.capability.validate(self.gateway_url)
        if (
            not self.active or self.claimed or self.verifier.capability != self.capability
            or owner != self.owner or surface != self.surface or deployment != self.deployment
            or target != self.target or _body(payload) != self.body
        ):
            raise QuotaError("Gateway attempt binding does not match.", code=409)
        return self.capability.envelope

    def claim(self, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, str]:
        if self.claimed:
            raise QuotaError("Gateway attempt was already dispatched.", code=409)
        self.match(
            self.surface, payload, deployment=self.deployment, target=self.target, owner=self.owner,
        )
        if any(name.lower() in INTERNAL_HEADERS for name in headers):
            raise QuotaError("Caller-supplied gateway attempt metadata is forbidden.", code=400)
        # No await between the check and the claim. Cancellation, lost replies and
        # errors never restore this permission, even if no body was received.
        self.claimed = True
        # No ContextVar token may survive a yielded SSE chunk: ASGI can close
        # the generator in another task. Authority ends before the send await.
        self.unbind()
        digest = hashlib.sha256(self.body).hexdigest()
        return {**headers, ATTEMPT_HEADER: f"{ATTEMPT_VERSION}.{self.nonce}.{digest}"}

    def check_response(self, response: httpx.Response) -> None:
        expected = f"{ATTEMPT_VERSION}.{self.nonce}"
        if response.headers.get(ACK_HEADER) != expected or 300 <= response.status_code < 400:
            raise httpx.RemoteProtocolError(
                "Gateway attempt acknowledgement is unavailable.", request=response.request,
            )


def current_attempt_envelope(
    surface: Surface, payload: dict[str, Any], *, deployment: str | None,
    target: str | None, owner: str,
) -> AttemptEnvelope | None:
    attempt = _prepared.get()
    if attempt is None or not no_replay_selected():
        return None
    return attempt.match(surface, payload, deployment=deployment, target=target, owner=owner)


@asynccontextmanager
async def prepare_attempt(
    surface: Surface, payload: dict[str, Any], *, deployment: str, target: str,
    gateway_url: str, owner: str | None, verifier: GatewayCapabilityVerifier | None,
    credential_header: str, has_credential: bool,
    staged: bool, api: str,
) -> AsyncIterator[PreparedAttempt | None]:
    selected = _selected.get()
    if selected is None:
        yield None
        return
    if owner is None or selected != owner:
        raise QuotaError("Gateway attempt has no matching authenticated owner.", code=403)
    if not staged:
        raise QuotaError("Versioned gateway staging is disabled.")
    if not has_credential or credential_header.lower() != "s7p-key":
        raise QuotaError("Bounded gateway transport requires authenticated proxy ingress.")
    capability = verifier.capability if verifier is not None else None
    if capability is None or verifier is None:
        raise QuotaError("Verified gateway attempt capability is unavailable.")
    capability.validate(gateway_url)
    url, base = httpx.URL(target), httpx.URL(gateway_url)
    relative = url.path.removeprefix(ATTEMPT_PATH)
    route = (
        (surface == "chat" and api == "responses" and relative == "/responses" and payload.get("model") == deployment)
        or (surface == "chat" and api == "chat" and relative == f"/deployments/{deployment}/chat/completions")
        or (surface == "embedding" and relative == f"/deployments/{deployment}/embeddings")
    )
    if (
        url.scheme != base.scheme or url.host != base.host or url.port != base.port
        or not url.path.startswith(ATTEMPT_PATH + "/") or not route or url.userinfo or url.fragment
        or url.raw_path.split(b"?", 1)[0] != url.path.encode("ascii", errors="replace")
        or (url.query and re.fullmatch(rb"api-version=[A-Za-z0-9_.-]{1,64}", url.query) is None)
        or re.fullmatch(r"[A-Za-z0-9_.-]+", deployment) is None
        or ("model" in payload and payload["model"] != deployment)
        or not supported_attempt_payload(surface, payload)
    ):
        raise QuotaError("Unsupported bounded gateway operation.")
    from ..hard_quota.dispatch import current_dispatch_catalog

    catalog = current_dispatch_catalog()
    model = model_for_deployment(catalog, deployment) if catalog is not None else None
    if model is None or model.api not in {"chat", "responses", "embedding"}:
        raise QuotaError("Unsupported versioned gateway provider.")
    if relative == "/responses":
        maximum = payload.get("max_output_tokens")
        if (
            model is None or model.maxOutputTokens is None or model.maxOutputTokens <= 0
            or type(maximum) is not int or not 0 < maximum <= model.maxOutputTokens
        ):
            raise QuotaError("Bounded Responses output maximum is outside the catalog limit.")
    encoded = _body(payload)
    await verifier.verify(capability)
    attempt = PreparedAttempt(
        owner, surface, deployment, target, encoded, capability, verifier, gateway_url,
    )
    attempt.binding = _prepared.set(attempt)
    try:
        yield attempt
    finally:
        attempt.active = False
        attempt.unbind()


def bounded_http_client(timeout: float) -> httpx.AsyncClient:
    """No inherited redirects, auth challenges, mounts, proxy, hooks or retries."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=0, http1=True, http2=False, trust_env=False),
        timeout=timeout, follow_redirects=False, trust_env=False,
    )
