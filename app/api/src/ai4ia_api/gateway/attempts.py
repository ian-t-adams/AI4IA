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
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ..hard_quota.coverage import AttemptEnvelope, supported_attempt_payload
from ..hard_quota.models import QuotaError, Surface

ATTEMPT_VERSION = "ai4ia-one-attempt-v1"
ATTEMPT_HEADER = "x-ai4ia-attempt"
PROXY_PROOF_HEADER = "x-ai4ia-proxy-attempt"
ACK_HEADER = "x-ai4ia-attempt-ack"
INTERNAL_HEADERS = frozenset({ATTEMPT_HEADER, PROXY_PROOF_HEADER, ACK_HEADER})
MAX_BODY_BYTES = 1024 * 1024


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
    version: str = ATTEMPT_VERSION

    def validate(self, gateway_url: str) -> None:
        url = httpx.URL(self.gateway_url)
        if (
            self.version != ATTEMPT_VERSION
            or self.gateway_url != gateway_url
            or url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.proxy_image) is None
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
        """Refuse stale/unknown/mismatched deployed evidence before admission."""
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
    if attempt is None:
        return None
    return attempt.match(surface, payload, deployment=deployment, target=target, owner=owner)


@asynccontextmanager
async def prepare_attempt(
    surface: Surface, payload: dict[str, Any], *, deployment: str, target: str,
    gateway_url: str, owner: str | None, verifier: GatewayCapabilityVerifier | None,
    credential_header: str, has_credential: bool,
) -> AsyncIterator[PreparedAttempt | None]:
    selected = _selected.get()
    if selected is None:
        # An unrelated nested metered call cannot inherit a parent's proof.
        token = _prepared.set(None)
        try:
            yield None
        finally:
            _prepared.reset(token)
        return
    if owner is None or selected != owner:
        raise QuotaError("Gateway attempt has no matching authenticated owner.", code=403)
    if not has_credential or credential_header.lower() != "s7p-key":
        raise QuotaError("Bounded gateway transport requires authenticated proxy ingress.")
    capability = verifier.capability if verifier is not None else None
    if capability is None or verifier is None:
        raise QuotaError("Verified gateway attempt capability is unavailable.")
    capability.validate(gateway_url)
    url, base = httpx.URL(target), httpx.URL(gateway_url)
    relative = url.path.removeprefix(base.path)
    route = (
        (surface == "chat" and relative == "/responses" and payload.get("model") == deployment)
        or (surface == "chat" and relative == f"/deployments/{deployment}/chat/completions")
        or (surface == "embedding" and relative == f"/deployments/{deployment}/embeddings")
    )
    if (
        url.scheme != base.scheme or url.host != base.host or url.port != base.port
        or not url.path.startswith(base.path + "/") or not route or url.userinfo or url.fragment
        or any(key != "api-version" for key in url.params) or len(url.params.multi_items()) > 1
        or re.fullmatch(r"[A-Za-z0-9_.-]+", deployment) is None
        or ("model" in payload and payload["model"] != deployment)
        or not supported_attempt_payload(surface, payload)
    ):
        raise QuotaError("Unsupported bounded gateway operation.")
    encoded = _body(payload)
    await verifier.verify(capability)
    attempt = PreparedAttempt(
        owner, surface, deployment, target, encoded, capability, verifier, gateway_url,
    )
    token = _prepared.set(attempt)
    try:
        yield attempt
    finally:
        attempt.active = False
        _prepared.reset(token)


def bounded_http_client(timeout: float) -> httpx.AsyncClient:
    """No inherited redirects, auth challenges, mounts, proxy, hooks or retries."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=0, http1=True, http2=False, trust_env=False),
        timeout=timeout, follow_redirects=False, trust_env=False,
    )
