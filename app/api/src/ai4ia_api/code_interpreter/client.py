"""Async Code Interpreter client over the Azure OpenAI Responses API (Phase 11C).

Verified against Microsoft Learn (Azure OpenAI Responses, v1 surface):

- Submit: ``POST {base}/openai/v1/responses`` with a JSON body
  ``{model, instructions, input, tools:[{type:"code_interpreter",
  container:{type:"auto"}}]}``. The call is synchronous — the response returns the
  completed turn (``status:"completed"``) with a top-level ``output_text`` plus an
  ``output`` array (assistant message + ``code_interpreter_call`` items). No
  polling is required (unlike Content Understanding).
- The v1 GA path omits ``api-version``; ``?api-version=preview`` opts into latest
  preview features (kept configurable, default empty).

Auth mirrors the rest of the app: ``api_key`` sends the resource key in the
``api-key`` header (the header the Learn curl/SDK samples use for the v1 path);
``bearer`` sends a static key as a bearer when configured, otherwise an AAD
managed-identity token. The AAD scope for the v1 endpoint is
``https://ai.azure.com/.default`` (verified against the Learn Entra samples for
``/openai/v1``), overridable via settings.

``httpx`` and the azure SDK are injected/lazy so tests run without network or the
azure libraries (a fake client is injected in unit tests).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

import httpx

from ..config import GatewayAuthMode, Settings
from .models import CodeInterpreterResult, parse_response

logger = logging.getLogger(__name__)

_TOKEN_REFRESH_MARGIN_S = 300

# The built-in Code Interpreter tool spec (Azure-managed automatic container).
CODE_INTERPRETER_TOOL: dict[str, Any] = {
    "type": "code_interpreter",
    "container": {"type": "auto"},
}


class CITokenProvider(Protocol):
    async def __call__(self) -> str: ...


class _AadTokenProvider:
    """Caches + refreshes an AAD token for the v1 Responses endpoint, single-flight.

    The credential is created lazily on first call so importing this module never
    requires the azure SDK; tests inject a provider and never reach this path.
    """

    def __init__(self, scope: str, credential: Any | None = None) -> None:
        self._scope = scope
        self._credential = credential
        self._owns_credential = credential is None
        self._token: Any | None = None
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        tok = self._token
        return tok is not None and (tok.expires_on - time.time()) > _TOKEN_REFRESH_MARGIN_S

    async def __call__(self) -> str:
        if self._fresh():
            return self._token.token  # type: ignore[union-attr]
        async with self._lock:
            if self._fresh():
                return self._token.token  # type: ignore[union-attr]
            if self._credential is None:
                from azure.identity.aio import DefaultAzureCredential

                self._credential = DefaultAzureCredential()
            self._token = await self._credential.get_token(self._scope)
            return self._token.token

    async def close(self) -> None:
        if self._owns_credential and self._credential is not None:
            close = getattr(self._credential, "close", None)
            if close is not None:
                await close()


class CodeInterpreterError(Exception):
    """An upstream Code Interpreter error (non-2xx or transport failure)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"code interpreter error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class CodeInterpreterClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        token_provider: CITokenProvider | None = None,
    ) -> None:
        self._base = (settings.code_interpreter_base_url or "").rstrip("/")
        self._model = settings.code_interpreter_model or ""
        self._api_version = settings.code_interpreter_api_version
        self._auth_mode = settings.code_interpreter_auth_mode
        self._api_key = settings.code_interpreter_api_key
        self._scope = settings.code_interpreter_aad_scope
        self._timeout = settings.code_interpreter_timeout_seconds
        self._http = http_client
        self._token_provider = token_provider
        self._owns_token_provider = token_provider is None

    def responses_url(self) -> str:
        url = f"{self._base}/openai/v1/responses"
        if self._api_version:
            url = f"{url}?api-version={self._api_version}"
        return url

    async def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_mode == GatewayAuthMode.api_key and self._api_key:
            headers["api-key"] = self._api_key
        elif self._auth_mode == GatewayAuthMode.bearer:
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            else:
                headers["Authorization"] = f"Bearer {await self._token()}"
        return headers

    async def _token(self) -> str:
        if self._token_provider is None:
            self._token_provider = _AadTokenProvider(self._scope)
        return await self._token_provider()

    def _client(self) -> tuple[httpx.AsyncClient, bool]:
        if self._http is not None:
            return self._http, False
        return httpx.AsyncClient(timeout=self._timeout), True

    async def run(self, *, instructions: str, user_input: str) -> CodeInterpreterResult:
        """Run one Code Interpreter turn and return the normalized result.

        Raises :class:`CodeInterpreterError` on an upstream/transport error. One
        ``httpx`` client (and TLS connection) is used for the single POST.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "tools": [CODE_INTERPRETER_TOOL],
            "instructions": instructions,
            "input": user_input,
        }
        client, owned = self._client()
        try:
            headers = await self._auth_headers()
            try:
                resp = await client.post(
                    self.responses_url(), headers=headers, json=payload
                )
            except httpx.HTTPError as exc:
                raise CodeInterpreterError(0, str(exc)) from exc
            if resp.status_code >= 400:
                raise CodeInterpreterError(resp.status_code, resp.text)
            try:
                body = resp.json()
            except ValueError as exc:
                raise CodeInterpreterError(resp.status_code, "non-JSON response") from exc
            if not isinstance(body, dict):
                raise CodeInterpreterError(resp.status_code, "unexpected response shape")
            return parse_response(body)
        finally:
            if owned:
                await client.aclose()

    async def close(self) -> None:
        if self._owns_token_provider and self._token_provider is not None:
            close = getattr(self._token_provider, "close", None)
            if close is not None:
                await close()
