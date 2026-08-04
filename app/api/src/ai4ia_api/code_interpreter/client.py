"""Async Code Interpreter client over the Azure OpenAI Responses API.

Verified against Microsoft Learn (Azure OpenAI Responses, v1 surface):

- Submit: ``POST {base}/openai/v1/responses`` with a JSON body
  ``{model, instructions, input, tools:[{type:"code_interpreter",
  container:{type:"auto"}}]}``. The call is synchronous — the response returns the
  completed turn (``status:"completed"``) with a top-level ``output_text`` plus an
  ``output`` array (assistant message + ``code_interpreter_call`` items). No
  polling is required (unlike Content Understanding).
- The v1 GA path omits ``api-version``; ``?api-version=preview`` opts into latest
  preview features (kept configurable, default empty).
- Raw file inputs (optional): :meth:`upload_file` POSTs an original file to
  ``{base}/openai/v1/files`` (``purpose=assistants``) and returns its ``file_id``;
  passing those ids to :meth:`run` seeds the sandbox container
  (``container.file_ids``) so the model reads the real file. :meth:`delete_file`
  cleans the upload up afterwards (best-effort). Verified against Microsoft Learn
  (Azure OpenAI Responses — Code Interpreter containers + "Upload PDF and analyze").

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
from collections.abc import Sequence
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

# Purpose used when uploading an original file to the Responses Files API for the
# code interpreter. Verified on Microsoft Learn (Responses "Upload PDF and analyze"):
# ``assistants`` is supported; ``user_data`` is not.
_FILE_UPLOAD_PURPOSE = "assistants"


def code_interpreter_tool(file_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """The ``code_interpreter`` tool spec, optionally seeding the auto container
    with uploaded ``file_ids`` so the sandbox can read the real uploaded files."""
    if not file_ids:
        return CODE_INTERPRETER_TOOL
    return {
        "type": "code_interpreter",
        "container": {"type": "auto", "file_ids": list(file_ids)},
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
            return self._token.token  # pyright: ignore[reportOptionalMemberAccess]
        async with self._lock:
            if self._fresh():
                return self._token.token  # pyright: ignore[reportOptionalMemberAccess]
            if self._credential is None:
                from azure.identity.aio import DefaultAzureCredential

                self._credential = DefaultAzureCredential()
            self._token = await self._credential.get_token(self._scope)
            return self._token.token  # pyright: ignore[reportOptionalMemberAccess]

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

    def files_url(self, file_id: str | None = None) -> str:
        """The Files API endpoint (v1 surface): the collection, or one file."""
        url = f"{self._base}/openai/v1/files"
        if file_id:
            url = f"{url}/{file_id}"
        if self._api_version:
            url = f"{url}?api-version={self._api_version}"
        return url

    async def _auth_headers(self, *, json_body: bool = True) -> dict[str, str]:
        # Only set Content-Type for JSON bodies; a multipart upload lets httpx set
        # the multipart boundary content-type itself.
        headers: dict[str, str] = {"Content-Type": "application/json"} if json_body else {}
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

    async def run(
        self,
        *,
        instructions: str,
        user_input: str,
        file_ids: Sequence[str] | None = None,
    ) -> CodeInterpreterResult:
        """Run one Code Interpreter turn and return the normalized result.

        When ``file_ids`` are supplied they seed the sandbox container so the model
        can read the real uploaded files; otherwise the default auto container is
        used. Raises :class:`CodeInterpreterError` on an upstream/transport error.
        One ``httpx`` client (and TLS connection) is used for the single POST.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "tools": [code_interpreter_tool(file_ids)],
            "instructions": instructions,
            "input": user_input,
            # Match the Responses gateway's retention posture (see
            # gateway/client.py::build_responses_request). ``store`` defaults to
            # TRUE on this surface, so without this every compute turn leaves a
            # provider-side copy of the user's instructions, input and output
            # retrievable from ``GET /responses/{id}`` outside the app. This path
            # is the documented direct-to-Foundry exception, which makes it the
            # one place the gateway's opt-out would not otherwise apply.
            "store": False,
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

    async def upload_file(
        self, *, filename: str, content: bytes, content_type: str | None = None
    ) -> str:
        """Upload one file to the Responses Files API and return its ``file_id``.

        Multipart POST to ``{base}/openai/v1/files`` with ``purpose=assistants`` (the
        purpose accepted for code-interpreter inputs). Raises
        :class:`CodeInterpreterError` on an upstream/transport error or a missing id.
        """
        client, owned = self._client()
        try:
            headers = await self._auth_headers(json_body=False)
            files = {
                "file": (
                    filename or "file",
                    content,
                    content_type or "application/octet-stream",
                )
            }
            try:
                resp = await client.post(
                    self.files_url(),
                    headers=headers,
                    files=files,
                    data={"purpose": _FILE_UPLOAD_PURPOSE},
                )
            except httpx.HTTPError as exc:
                raise CodeInterpreterError(0, str(exc)) from exc
            if resp.status_code >= 400:
                raise CodeInterpreterError(resp.status_code, resp.text)
            try:
                body = resp.json()
            except ValueError as exc:
                raise CodeInterpreterError(resp.status_code, "non-JSON response") from exc
            file_id = body.get("id") if isinstance(body, dict) else None
            if not isinstance(file_id, str) or not file_id:
                raise CodeInterpreterError(resp.status_code, "file upload returned no id")
            return file_id
        finally:
            if owned:
                await client.aclose()

    async def delete_file(self, file_id: str) -> bool:
        """Best-effort delete of an uploaded file. Never raises; returns success."""
        if not file_id:
            return False
        client, owned = self._client()
        try:
            try:
                headers = await self._auth_headers(json_body=False)
                resp = await client.delete(self.files_url(file_id), headers=headers)
            except Exception:  # noqa: BLE001 - cleanup must never break the turn
                logger.debug("code interpreter file cleanup failed", exc_info=True)
                return False
            return getattr(resp, "status_code", 500) < 400
        finally:
            if owned:
                await client.aclose()

    async def close(self) -> None:
        if self._owns_token_provider and self._token_provider is not None:
            close = getattr(self._token_provider, "close", None)
            if close is not None:
                await close()
