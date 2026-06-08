"""HTTP client that sends chat completions through the model gateway.

Request shaping is explicit (``provider_style``) so we never confuse the
Azure/Foundry-native shape (deployment in path + ``api-version`` query) with the
OpenAI-compatible shape (``model`` in body). URL construction is pure and
unit-tested; the exact APIM/proxy/Foundry path prefix is parameterized so it can
be fixed at integration time without code changes.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import GatewayAuthMode, GatewayProviderStyle, Settings


class ModelGatewayError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"gateway error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass
class GatewayRequest:
    url: str
    headers: dict[str, str]
    json: dict[str, Any]


@dataclass
class ChatChunk:
    """A single streamed SSE event: ``delta`` is the assistant text increment;
    ``raw`` is the original ``data:`` payload for passthrough; ``done`` marks the
    terminal ``[DONE]`` sentinel; ``usage`` carries the token-usage object when a
    chunk reports it (the final, empty-``choices`` usage chunk emitted when
    ``stream_options.include_usage`` is set)."""

    delta: str = ""
    raw: str = ""
    done: bool = False
    usage: dict[str, Any] | None = None


def _default_chat_path(style: GatewayProviderStyle) -> str:
    if style == GatewayProviderStyle.azure_openai_native:
        return "/deployments/{deployment}/chat/completions"
    return "/chat/completions"


def _default_embeddings_path(style: GatewayProviderStyle) -> str:
    if style == GatewayProviderStyle.azure_openai_native:
        return "/deployments/{deployment}/embeddings"
    return "/embeddings"


class ModelGatewayClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._base = settings.model_gateway_url.rstrip("/")
        self._style = settings.gateway_provider_style
        self._api_version = settings.gateway_api_version
        self._auth_mode = settings.model_gateway_auth_mode
        self._api_key = settings.model_gateway_api_key
        self._timeout = settings.gateway_timeout_seconds
        self._chat_path = settings.gateway_chat_path or _default_chat_path(self._style)
        self._embeddings_path = _default_embeddings_path(self._style)
        self._stream_include_usage = settings.gateway_stream_include_usage
        self._http = http_client

    def _auth_headers(self, correlation_id: str | None) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if correlation_id:
            headers["x-correlation-id"] = correlation_id
        if self._auth_mode == GatewayAuthMode.api_key and self._api_key:
            headers["Ocp-Apim-Subscription-Key"] = self._api_key
        elif self._auth_mode == GatewayAuthMode.bearer and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def build_request(
        self,
        *,
        deployment: str,
        messages: Sequence[dict[str, Any]],
        params: dict[str, Any] | None = None,
        stream: bool = False,
        include_usage: bool = False,
        correlation_id: str | None = None,
    ) -> GatewayRequest:
        path = self._chat_path.format(deployment=deployment)
        url = f"{self._base}{path if path.startswith('/') else '/' + path}"

        body: dict[str, Any] = {"messages": list(messages), **(params or {})}
        if stream:
            body["stream"] = True
            # Set after merging caller params so it can't be accidentally
            # overridden; only requested when streaming + enabled.
            if include_usage:
                body["stream_options"] = {"include_usage": True}
            else:
                body.pop("stream_options", None)
        if self._style == GatewayProviderStyle.azure_openai_native:
            url = f"{url}?api-version={self._api_version}"
        else:
            body["model"] = deployment

        headers = self._auth_headers(correlation_id)

        return GatewayRequest(url=url, headers=headers, json=body)

    def build_embed_request(
        self,
        *,
        deployment: str,
        inputs: Sequence[str],
        correlation_id: str | None = None,
    ) -> GatewayRequest:
        """Build the embeddings request, routed through the same gateway/auth as
        chat (APIM forwards any path to the Foundry data plane)."""
        path = self._embeddings_path.format(deployment=deployment)
        url = f"{self._base}{path if path.startswith('/') else '/' + path}"
        body: dict[str, Any] = {"input": list(inputs)}
        if self._style == GatewayProviderStyle.azure_openai_native:
            url = f"{url}?api-version={self._api_version}"
        else:
            body["model"] = deployment
        return GatewayRequest(url=url, headers=self._auth_headers(correlation_id), json=body)

    def _client(self) -> tuple[httpx.AsyncClient, bool]:
        if self._http is not None:
            return self._http, False
        return httpx.AsyncClient(timeout=self._timeout), True

    async def complete(
        self,
        *,
        deployment: str,
        messages: Sequence[dict[str, Any]],
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        req = self.build_request(
            deployment=deployment,
            messages=messages,
            params=params,
            stream=False,
            correlation_id=correlation_id,
        )
        client, owned = self._client()
        try:
            resp = await client.post(req.url, headers=req.headers, json=req.json)
            if resp.status_code >= 400:
                raise ModelGatewayError(resp.status_code, resp.text)
            return resp.json()
        finally:
            if owned:
                await client.aclose()

    async def embed(
        self,
        *,
        deployment: str,
        inputs: Sequence[str],
        correlation_id: str | None = None,
    ) -> list[list[float]]:
        """Return one embedding vector per input, order-aligned to ``inputs``."""
        if not inputs:
            return []
        req = self.build_embed_request(
            deployment=deployment, inputs=inputs, correlation_id=correlation_id
        )
        client, owned = self._client()
        try:
            resp = await client.post(req.url, headers=req.headers, json=req.json)
            if resp.status_code >= 400:
                raise ModelGatewayError(resp.status_code, resp.text)
            data = resp.json().get("data") or []
            # The API may not guarantee order; sort by the declared index.
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            return [item.get("embedding") or [] for item in ordered]
        finally:
            if owned:
                await client.aclose()

    async def stream(
        self,
        *,
        deployment: str,
        messages: Sequence[dict[str, Any]],
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> AsyncIterator[ChatChunk]:
        client, owned = self._client()
        try:
            # Request token usage in the stream when enabled, but never let an
            # unsupported ``stream_options`` break streaming: if the FIRST attempt
            # is rejected with 400 (before any bytes are yielded), retry once
            # without it. 400 is the unsupported-parameter signal; other statuses
            # (401/403/429/5xx) are not param-related and propagate immediately.
            attempts = [True, False] if self._stream_include_usage else [False]
            for attempt_idx, include_usage in enumerate(attempts):
                req = self.build_request(
                    deployment=deployment,
                    messages=messages,
                    params=params,
                    stream=True,
                    include_usage=include_usage,
                    correlation_id=correlation_id,
                )
                is_last = attempt_idx == len(attempts) - 1
                async with client.stream(
                    "POST", req.url, headers=req.headers, json=req.json
                ) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        detail = body.decode("utf-8", "replace")
                        if include_usage and not is_last and resp.status_code == 400:
                            # Likely stream_options unsupported; fall back cleanly.
                            continue
                        raise ModelGatewayError(resp.status_code, detail)
                    async for line in resp.aiter_lines():
                        chunk = parse_sse_line(line)
                        if chunk is not None:
                            yield chunk
                            if chunk.done:
                                return
                    return
        finally:
            if owned:
                await client.aclose()


def parse_sse_line(line: str) -> ChatChunk | None:
    """Parse one SSE line into a ChatChunk (None for blanks/comments)."""
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload:
        return None
    if payload == "[DONE]":
        return ChatChunk(done=True, raw=payload)
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return ChatChunk(raw=payload)
    delta = ""
    for choice in obj.get("choices", []):
        piece = (choice.get("delta") or {}).get("content")
        if piece:
            delta += piece
    # The final usage chunk (when include_usage is set) has empty ``choices`` and
    # a populated ``usage`` object; surface it so the caller can meter the turn.
    return ChatChunk(delta=delta, raw=payload, usage=obj.get("usage"))
