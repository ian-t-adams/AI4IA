"""Async Content Understanding REST client.

Verified against Microsoft Learn (2025-11-01 GA and selected
2026-06-01-preview operations):

- Submit bytes: ``POST {base}/contentunderstanding/analyzers/{analyzerId}:analyzeBinary
  ?api-version=...`` with the raw bytes as the body and the file ``Content-Type``.
  A 202 returns the ``Operation-Location`` response header.
- Poll: ``GET {operation-location}`` → ``200 {id, status, result}``; ``status`` is
  ``NotStarted``/``Running`` until terminal (``Succeeded``/``Failed``).
- Synchronous Read/Layout: ``POST …:{analyzeBinaryInline}`` returns the result
  directly and never produces or polls an operation URL.

Auth mirrors the model gateway: ``api_key`` sends the CU resource key in
``Ocp-Apim-Subscription-Key``; ``bearer`` sends a static key as a bearer when one
is configured, otherwise an AAD managed-identity token (Cognitive Services scope).
``httpx`` and the azure SDK are injected/lazy so tests run without network or the
azure libraries.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

from ..config import GatewayAuthMode, Settings
from ..http_retry import request_with_retry
from .models import (
    CU_PREVIEW_API_VERSION,
    TERMINAL_STATES,
    CUResult,
    is_valid_analyzer_id,
    parse_result,
)

logger = logging.getLogger(__name__)

# AAD scope for an Azure AI Services / Cognitive Services access token.
_CS_SCOPE = "https://cognitiveservices.azure.com/.default"
_TOKEN_REFRESH_MARGIN_S = 300


class CUTokenProvider(Protocol):
    async def __call__(self) -> str: ...


class _AadTokenProvider:
    """Caches + refreshes an AAD token for Cognitive Services, single-flight.

    The credential is created lazily on first call so importing this module never
    requires the azure SDK; tests inject a provider and never reach this path.
    """

    def __init__(self, credential: Any | None = None) -> None:
        self._credential = credential
        self._owns_credential = credential is None
        self._token: Any | None = None
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        tok = self._token
        return tok is not None and (tok.expires_on - time.time()) > _TOKEN_REFRESH_MARGIN_S

    async def __call__(self) -> str:
        if self._fresh():
            return self._token.token  # pyright: ignore[reportOptionalMemberAccess]
        async with self._lock:
            if self._fresh():
                return self._token.token  # pyright: ignore[reportOptionalMemberAccess]
            if self._credential is None:
                from azure.identity.aio import DefaultAzureCredential

                self._credential = DefaultAzureCredential()
            self._token = await self._credential.get_token(_CS_SCOPE)
            return self._token.token  # pyright: ignore[reportOptionalMemberAccess]

    async def close(self) -> None:
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                await close()


class ContentUnderstandingError(Exception):
    """An upstream CU error (non-2xx, missing Operation-Location, or timeout)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"content understanding error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _http_error_detail(resp: httpx.Response) -> str:
    """Bounded provider-owned code only; response messages can echo document data."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code"):
            return f"upstream code={str(error['code'])[:80]}"
        if body.get("code"):
            return f"upstream code={str(body['code'])[:80]}"
    return f"upstream status={resp.status_code}"


class ContentUnderstandingClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        token_provider: CUTokenProvider | None = None,
    ) -> None:
        self._base = (settings.cu_base_url or "").rstrip("/")
        self._api_version = settings.cu_api_version
        self._auth_mode = settings.cu_auth_mode
        self._api_key = settings.cu_api_key
        self._timeout = settings.cu_timeout_seconds
        self._poll_interval = settings.cu_poll_interval_seconds
        self._max_poll = settings.cu_max_poll_seconds
        # Transient-retry policy for the idempotent GET poll only. The submit POST
        # starts a new analyze operation and is never retried — see
        # ai4ia_api.http_retry.
        self._retry_policy = settings.outbound_retry_policy()
        self._http = http_client
        self._token_provider = token_provider
        self._owns_token_provider = token_provider is None

    def analyzer_url(
        self,
        analyzer_id: str,
        *,
        action: str,
        api_version: str | None = None,
    ) -> str:
        # Defense in depth: the request model validates this at creation time,
        # but the ``Analyzer`` domain model itself does not, so a persisted
        # legacy id (or any other path that builds an ``Analyzer`` directly)
        # could otherwise reach this URL unvalidated.
        if not is_valid_analyzer_id(analyzer_id):
            raise ValueError(f"invalid content understanding analyzer id: {analyzer_id!r}")
        return (
            f"{self._base}/contentunderstanding/analyzers/{analyzer_id}"
            f":{action}?api-version={api_version or self._api_version}"
        )

    def submit_url(
        self, analyzer_id: str, *, api_version: str | None = None
    ) -> str:
        return self.analyzer_url(
            analyzer_id, action="analyzeBinary", api_version=api_version
        )

    def inline_url(
        self, analyzer_id: str, *, api_version: str = CU_PREVIEW_API_VERSION
    ) -> str:
        return self.analyzer_url(
            analyzer_id, action="analyzeBinaryInline", api_version=api_version
        )

    def get_analyzer_url(
        self, analyzer_id: str, *, api_version: str | None = None
    ) -> str:
        if not is_valid_analyzer_id(analyzer_id):
            raise ValueError(f"invalid content understanding analyzer id: {analyzer_id!r}")
        return (
            f"{self._base}/contentunderstanding/analyzers/{analyzer_id}"
            f"?api-version={api_version or self._api_version}"
        )

    async def _auth_headers(self, content_type: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        if self._auth_mode == GatewayAuthMode.api_key and self._api_key:
            headers["Ocp-Apim-Subscription-Key"] = self._api_key
        elif self._auth_mode == GatewayAuthMode.bearer:
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            else:
                headers["Authorization"] = f"Bearer {await self._token()}"
        return headers

    async def _token(self) -> str:
        if self._token_provider is None:
            self._token_provider = _AadTokenProvider()
        return await self._token_provider()

    def _client(self) -> tuple[httpx.AsyncClient, bool]:
        if self._http is not None:
            return self._http, False
        return httpx.AsyncClient(timeout=self._timeout), True

    async def _submit_binary(
        self,
        client: httpx.AsyncClient,
        analyzer_id: str,
        data: bytes,
        content_type: str,
        api_version: str | None = None,
    ) -> str:
        url = self.submit_url(analyzer_id, api_version=api_version)
        headers = await self._auth_headers(content_type or "application/octet-stream")
        resp = await client.post(url, headers=headers, content=data)
        if resp.status_code >= 400:
            raise ContentUnderstandingError(
                resp.status_code, _http_error_detail(resp)
            )
        op = resp.headers.get("operation-location") or resp.headers.get(
            "Operation-Location"
        )
        if not op:
            raise ContentUnderstandingError(
                resp.status_code, "response missing Operation-Location header"
            )
        return op

    async def _poll_once(
        self, client: httpx.AsyncClient, operation_url: str
    ) -> dict[str, Any]:
        headers = await self._auth_headers()
        resp = await request_with_retry(
            lambda: client.get(operation_url, headers=headers),
            method="GET",
            policy=self._retry_policy,
        )
        if resp.status_code >= 400:
            raise ContentUnderstandingError(
                resp.status_code, _http_error_detail(resp)
            )
        return resp.json()

    async def submit_binary(
        self,
        analyzer_id: str,
        data: bytes,
        content_type: str,
        *,
        api_version: str | None = None,
    ) -> str:
        """POST the bytes and return the ``Operation-Location`` poll URL."""
        client, owned = self._client()
        try:
            return await self._submit_binary(
                client, analyzer_id, data, content_type, api_version
            )
        finally:
            if owned:
                await client.aclose()

    async def poll_once(self, operation_url: str) -> dict[str, Any]:
        client, owned = self._client()
        try:
            return await self._poll_once(client, operation_url)
        finally:
            if owned:
                await client.aclose()

    async def get_analyzer(
        self, analyzer_id: str, *, api_version: str | None = None
    ) -> dict[str, Any]:
        client, owned = self._client()
        try:
            headers = await self._auth_headers()
            resp = await request_with_retry(
                lambda: client.get(
                    self.get_analyzer_url(
                        analyzer_id, api_version=api_version
                    ),
                    headers=headers,
                ),
                method="GET",
                policy=self._retry_policy,
            )
            if resp.status_code >= 400:
                raise ContentUnderstandingError(
                    resp.status_code, _http_error_detail(resp)
                )
            body = resp.json()
            if not isinstance(body, dict):
                raise ContentUnderstandingError(
                    502, "analyzer response was not an object"
                )
            return body
        finally:
            if owned:
                await client.aclose()

    async def analyze_inline(
        self,
        analyzer_id: str,
        data: bytes,
        content_type: str,
        *,
        api_version: str = CU_PREVIEW_API_VERSION,
    ) -> CUResult:
        client, owned = self._client()
        try:
            headers = await self._auth_headers(
                content_type or "application/octet-stream"
            )
            resp = await client.post(
                self.inline_url(analyzer_id, api_version=api_version),
                headers=headers,
                content=data,
            )
            if resp.status_code >= 400:
                raise ContentUnderstandingError(
                    resp.status_code, _http_error_detail(resp)
                )
            body = resp.json()
            if not isinstance(body, dict):
                raise ContentUnderstandingError(
                    502, "synchronous analysis response was not an object"
                )
            return parse_result(body)
        finally:
            if owned:
                await client.aclose()

    async def analyze(
        self,
        analyzer_id: str,
        data: bytes,
        content_type: str,
        *,
        api_version: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> CUResult:
        """Submit + poll until the operation reaches a terminal state.

        Raises :class:`ContentUnderstandingError` on an upstream error or if the
        poll budget (``cu_max_poll_seconds``) is exhausted. One ``httpx`` client
        (and TLS connection) is reused across the submit + every poll.
        """
        client, owned = self._client()
        try:
            operation_url = await self._submit_binary(
                client, analyzer_id, data, content_type, api_version
            )
            deadline = time.monotonic() + self._max_poll
            while True:
                body = await self._poll_once(client, operation_url)
                status = str(body.get("status", "")).lower()
                if status in TERMINAL_STATES:
                    return parse_result(body)
                if time.monotonic() >= deadline:
                    raise ContentUnderstandingError(
                        408, "content understanding analyze timed out"
                    )
                await sleep(self._poll_interval)
        finally:
            if owned:
                await client.aclose()

    async def close(self) -> None:
        if self._owns_token_provider and self._token_provider is not None:
            close = getattr(self._token_provider, "close", None)
            if close is not None:
                await close()
