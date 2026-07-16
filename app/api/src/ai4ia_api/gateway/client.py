"""HTTP client that sends chat completions through the model gateway.

Request shaping is explicit (``provider_style``) so we never confuse the
Azure/Foundry-native shape (deployment in path + ``api-version`` query) with the
OpenAI-compatible shape (``model`` in body). URL construction is pure and
unit-tested; the exact APIM/proxy/Foundry path prefix is parameterized so it can
be fixed at integration time without code changes.
"""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import GatewayAuthMode, GatewayProviderStyle, Settings
from ..http_retry import request_with_retry

# Azure OpenAI reasoning models (the GPT-5 family and the o-series) reject the
# classic Chat Completions sampling/limit parameters: they require
# ``max_completion_tokens`` instead of ``max_tokens`` and 400 on non-default
# ``temperature``/``top_p``/penalties/logprobs. A deployment name always begins
# with the catalog model id (e.g. ``gpt-5.2-slurmfactory-eastus2-glbl``), so a
# leading-id match is a reliable signal. ``model-router`` is deliberately
# EXCLUDED: it accepts the standard parameter set and drops the unsupported ones
# itself when it routes to an o-series model (per Microsoft Learn), so we must
# not pre-transform it.
_REASONING_DEPLOYMENT = re.compile(r"^(gpt-5|o1|o3|o4)\b", re.IGNORECASE)

# Sampling parameters the Chat Completions API rejects for reasoning models.
_REASONING_UNSUPPORTED_PARAMS = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "logprobs",
    "top_logprobs",
    "logit_bias",
)


def _is_reasoning_deployment(deployment: str) -> bool:
    return bool(_REASONING_DEPLOYMENT.match(deployment))


def _normalize_params_for_deployment(body: dict[str, Any], deployment: str) -> None:
    """In place: adapt chat params to a reasoning model's constraints.

    Translates ``max_tokens`` -> ``max_completion_tokens`` (preserving any
    caller-supplied ``max_completion_tokens``) and strips the sampling
    parameters that reasoning models reject. No-op for non-reasoning models, so
    gpt-4.1/4o and non-OpenAI deployments (e.g. DeepSeek) keep ``max_tokens``.
    """
    if not _is_reasoning_deployment(deployment):
        return
    if "max_tokens" in body:
        value = body.pop("max_tokens")
        if value is not None:
            body.setdefault("max_completion_tokens", value)
    for key in _REASONING_UNSUPPORTED_PARAMS:
        body.pop(key, None)


# --- Responses API (gpt-5-pro / gpt-5-codex / o3-pro) -----------------------
#
# Azure exposes a *separate* surface, the Responses API, for a handful of
# flagship reasoning models that 400 on chat/completions. It is reached at
# ``{base}/responses`` with the deployment name carried as ``model`` IN THE BODY
# (deployment-in-path returns 404 — the opposite of chat completions). The
# request/response schema also differs (``input``/``instructions`` in, an
# ``output`` array + ``input_tokens``/``output_tokens`` usage out), so the
# gateway translates both directions to keep the rest of the app speaking the
# single chat-completions shape.
_RESPONSES_PATH = "/responses"

# Reasoning models spend hidden "reasoning tokens" out of the same output budget
# as the visible answer, so a small ``max_output_tokens`` yields an EMPTY message
# with status ``incomplete``. The cap only *bounds* (never bills) unused tokens,
# so we floor it generously for Responses turns to keep short prompts from
# truncating to nothing; a caller asking for MORE is always honored.
_RESPONSES_MIN_OUTPUT_TOKENS = 16384


def _messages_to_responses_input(
    messages: Sequence[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Split chat-style messages into ``(instructions, input items)``.

    Every ``system`` message is concatenated (order-preserving) into the single
    Responses ``instructions`` field; user/assistant turns become ``input``
    items. AI4IA injects memory and uploaded-document context as ordered system
    blocks, so preserving order keeps the primary prompt's authority ahead of
    those untrusted blocks.
    """
    system_parts: list[str] = []
    items: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role == "system":
            if content:
                system_parts.append(content)
        else:
            items.append({"role": role, "content": content})
    instructions = "\n\n".join(system_parts) if system_parts else None
    return instructions, items


def _normalize_params_for_responses(params: dict[str, Any] | None) -> dict[str, Any]:
    """Map chat-completions params onto a Responses request body.

    - ``max_output_tokens``/``max_completion_tokens``/``max_tokens`` ->
      ``max_output_tokens`` (floored so reasoning models can't truncate to an
      empty message; a larger caller value wins).
    - ``reasoning_effort`` -> ``reasoning: {effort}``.
    - drops the sampling params reasoning models reject and the chat-only
      ``stream``/``stream_options`` keys (``stream`` is set by the builder).
    """
    out: dict[str, Any] = dict(params or {})
    max_out = out.pop("max_output_tokens", None)
    for key in ("max_completion_tokens", "max_tokens"):
        value = out.pop(key, None)
        if max_out is None:
            max_out = value
    try:
        floored = max(int(max_out), _RESPONSES_MIN_OUTPUT_TOKENS)
    except (TypeError, ValueError):
        floored = _RESPONSES_MIN_OUTPUT_TOKENS
    out["max_output_tokens"] = floored

    effort = out.pop("reasoning_effort", None)
    if effort:
        out["reasoning"] = {"effort": effort}

    for key in _REASONING_UNSUPPORTED_PARAMS:
        out.pop(key, None)
    out.pop("stream", None)
    out.pop("stream_options", None)
    return out


def _responses_text(obj: dict[str, Any]) -> str:
    """Concatenate all ``output_text`` fragments from a Responses ``output``."""
    parts: list[str] = []
    for item in obj.get("output") or []:
        if item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if block.get("type") == "output_text" and block.get("text"):
                parts.append(block["text"])
    return "".join(parts)


def _responses_usage_to_chat(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """Translate Responses usage -> chat-completions usage keys so the existing
    ``TokenUsage.parse`` meters Responses turns unchanged. Carries the reasoning
    token count through for audit (parse ignores unknown keys)."""
    if not isinstance(usage, dict):
        return None
    mapped: dict[str, Any] = {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        mapped["completion_tokens_details"] = {
            "reasoning_tokens": details["reasoning_tokens"]
        }
    return mapped


def _responses_json_to_chat(obj: dict[str, Any]) -> dict[str, Any]:
    """Translate a non-streamed Responses body into a chat-completions shape the
    router/metering already understand. ``status`` is preserved (``incomplete``
    means the answer was truncated but the tokens were still spent + billable)."""
    result: dict[str, Any] = {
        "choices": [
            {"message": {"role": "assistant", "content": _responses_text(obj)}}
        ],
        "_responses_status": obj.get("status"),
    }
    usage = _responses_usage_to_chat(obj.get("usage"))
    if usage is not None:
        result["usage"] = usage
    return result


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


def _default_images_path(style: GatewayProviderStyle) -> str:
    if style == GatewayProviderStyle.azure_openai_native:
        return "/deployments/{deployment}/images/generations"
    return "/images/generations"


def _default_speech_path(style: GatewayProviderStyle) -> str:
    if style == GatewayProviderStyle.azure_openai_native:
        return "/deployments/{deployment}/audio/speech"
    return "/audio/speech"


def _default_transcription_path(style: GatewayProviderStyle) -> str:
    if style == GatewayProviderStyle.azure_openai_native:
        return "/deployments/{deployment}/audio/transcriptions"
    return "/audio/transcriptions"


class ModelGatewayClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._base = settings.model_gateway_url.rstrip("/")
        self._style = settings.gateway_provider_style
        self._api_version = settings.gateway_api_version
        self._auth_mode = settings.model_gateway_auth_mode
        self._api_key = settings.model_gateway_api_key
        self._api_key_header = settings.model_gateway_api_key_header
        self._timeout = settings.gateway_timeout_seconds
        self._chat_path = settings.gateway_chat_path or _default_chat_path(self._style)
        self._embeddings_path = _default_embeddings_path(self._style)
        self._images_path = _default_images_path(self._style)
        self._speech_path = _default_speech_path(self._style)
        self._transcription_path = _default_transcription_path(self._style)
        self._stream_include_usage = settings.gateway_stream_include_usage
        self._image_api_version = settings.gateway_image_api_version
        self._image_timeout = settings.gateway_image_timeout_seconds
        self._video_api_version = settings.gateway_video_api_version
        self._video_timeout = settings.gateway_video_timeout_seconds
        self._audio_api_version = settings.gateway_audio_api_version
        self._audio_timeout = settings.gateway_audio_timeout_seconds
        # Transient-retry policy for the idempotent GET reads only (Sora job poll
        # + content download). Writes on this client (chat/embed/image/audio/job
        # create) are deliberately NOT retried — see ai4ia_api.http_retry.
        self._retry_policy = settings.outbound_retry_policy()
        self._http = http_client

    def _auth_headers(self, correlation_id: str | None) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if correlation_id:
            headers["x-correlation-id"] = correlation_id
        if self._auth_mode == GatewayAuthMode.api_key and self._api_key:
            headers[self._api_key_header] = self._api_key
        elif self._auth_mode == GatewayAuthMode.bearer and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _auth_headers_multipart(self, correlation_id: str | None) -> dict[str, str]:
        """Auth headers WITHOUT a Content-Type: httpx sets the multipart/form-data
        boundary itself for file uploads (transcription). Forcing application/json
        here would corrupt the multipart body."""
        headers: dict[str, str] = {}
        if correlation_id:
            headers["x-correlation-id"] = correlation_id
        if self._auth_mode == GatewayAuthMode.api_key and self._api_key:
            headers[self._api_key_header] = self._api_key
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
        _normalize_params_for_deployment(body, deployment)
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

    def build_responses_request(
        self,
        *,
        deployment: str,
        messages: Sequence[dict[str, Any]],
        params: dict[str, Any] | None = None,
        stream: bool = False,
        correlation_id: str | None = None,
    ) -> GatewayRequest:
        """Build a Responses API request (``POST {base}/responses``).

        Unlike chat completions, the deployment is the ``model`` field IN THE
        BODY for BOTH provider styles (deployment-in-path 404s on this surface);
        the Azure-native style still appends ``api-version``.
        """
        url = f"{self._base}{_RESPONSES_PATH}"
        instructions, input_items = _messages_to_responses_input(messages)
        body: dict[str, Any] = {
            "model": deployment,
            "input": input_items,
            **_normalize_params_for_responses(params),
        }
        if instructions:
            body["instructions"] = instructions
        if stream:
            body["stream"] = True
        if self._style == GatewayProviderStyle.azure_openai_native:
            url = f"{url}?api-version={self._api_version}"
        return GatewayRequest(
            url=url, headers=self._auth_headers(correlation_id), json=body
        )

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

    def build_image_request(
        self,
        *,
        deployment: str,
        prompt: str,
        size: str | None = None,
        n: int = 1,
        extra: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> GatewayRequest:
        """Build an image-generation request. Uses the image-specific api-version
        (image models may track a different supported version than chat) and the
        same gateway/auth path the chat + embeddings calls use.

        ``extra`` is for trusted, internally-constructed parameters only; the
        public router builds it from an explicit allowlist and never forwards
        arbitrary client keys.
        """
        path = self._images_path.format(deployment=deployment)
        url = f"{self._base}{path if path.startswith('/') else '/' + path}"
        body: dict[str, Any] = {"prompt": prompt, "n": n, **(extra or {})}
        if size:
            body["size"] = size
        if self._style == GatewayProviderStyle.azure_openai_native:
            url = f"{url}?api-version={self._image_api_version}"
        else:
            body["model"] = deployment
        return GatewayRequest(url=url, headers=self._auth_headers(correlation_id), json=body)

    async def generate_image(
        self,
        *,
        deployment: str,
        prompt: str,
        size: str | None = None,
        n: int = 1,
        extra: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Generate one or more images; returns the parsed provider JSON
        (``{data: [{b64_json}], usage, ...}``). Uses the longer image timeout."""
        req = self.build_image_request(
            deployment=deployment,
            prompt=prompt,
            size=size,
            n=n,
            extra=extra,
            correlation_id=correlation_id,
        )
        if self._http is not None:
            client, owned = self._http, False
        else:
            client, owned = httpx.AsyncClient(timeout=self._image_timeout), True
        try:
            resp = await client.post(
                req.url, headers=req.headers, json=req.json, timeout=self._image_timeout
            )
            if resp.status_code >= 400:
                raise ModelGatewayError(resp.status_code, resp.text)
            return resp.json()
        finally:
            if owned:
                await client.aclose()

    # --- Video generation ------------------------------------------------------
    # Sora is an async job API (NOT a single round-trip like images): create a
    # job, poll it to completion, then download the generation's MP4 bytes. The
    # three primitives below are intentionally thin; the submit -> poll ->
    # download orchestration (timeout, backoff, status interpretation) lives in
    # :class:`~ai4ia_api.videos.service.VideoGenerationService` so the gateway
    # stays a pure transport, mirroring the image gateway/service split.
    def _video_url(self, suffix: str) -> str:
        """Build a Sora video URL. ``suffix`` is the path under ``/v1/video``
        (e.g. ``/generations/jobs``). Appends the video api-version for the
        Azure-native style (the only style that serves the Sora job API)."""
        url = f"{self._base}/v1/video{suffix}"
        if self._style == GatewayProviderStyle.azure_openai_native:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}api-version={self._video_api_version}"
        return url

    async def create_video_job(
        self,
        *,
        deployment: str,
        prompt: str,
        width: int,
        height: int,
        n_seconds: int,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit a Sora video-generation job; returns the parsed job JSON
        (``{id, status, ...}``). The deployment is carried in the ``model`` body
        field (Sora's create API keys off the body, not the path)."""
        url = self._video_url("/generations/jobs")
        body: dict[str, Any] = {
            "model": deployment,
            "prompt": prompt,
            "width": width,
            "height": height,
            "n_seconds": n_seconds,
        }
        if self._http is not None:
            client, owned = self._http, False
        else:
            client, owned = httpx.AsyncClient(timeout=self._video_timeout), True
        try:
            resp = await client.post(
                url,
                headers=self._auth_headers(correlation_id),
                json=body,
                timeout=self._video_timeout,
            )
            if resp.status_code >= 400:
                raise ModelGatewayError(resp.status_code, resp.text)
            return resp.json()
        finally:
            if owned:
                await client.aclose()

    async def get_video_job(
        self, *, job_id: str, correlation_id: str | None = None
    ) -> dict[str, Any]:
        """Poll a Sora job's status; returns the parsed job JSON. On success the
        job carries ``generations[].id`` referencing the downloadable content."""
        url = self._video_url(f"/generations/jobs/{job_id}")
        if self._http is not None:
            client, owned = self._http, False
        else:
            client, owned = httpx.AsyncClient(timeout=self._video_timeout), True
        try:
            resp = await request_with_retry(
                lambda: client.get(
                    url,
                    headers=self._auth_headers(correlation_id),
                    timeout=self._video_timeout,
                ),
                method="GET",
                policy=self._retry_policy,
            )
            if resp.status_code >= 400:
                raise ModelGatewayError(resp.status_code, resp.text)
            return resp.json()
        finally:
            if owned:
                await client.aclose()

    async def get_video_content(
        self, *, generation_id: str, correlation_id: str | None = None
    ) -> bytes:
        """Download a completed generation's MP4 bytes."""
        url = self._video_url(f"/generations/{generation_id}/content/video")
        if self._http is not None:
            client, owned = self._http, False
        else:
            client, owned = httpx.AsyncClient(timeout=self._video_timeout), True
        try:
            resp = await request_with_retry(
                lambda: client.get(
                    url,
                    headers=self._auth_headers(correlation_id),
                    timeout=self._video_timeout,
                ),
                method="GET",
                policy=self._retry_policy,
            )
            if resp.status_code >= 400:
                raise ModelGatewayError(resp.status_code, resp.text)
            return resp.content
        finally:
            if owned:
                await client.aclose()

    def build_speech_request(
        self,
        *,
        deployment: str,
        text: str,
        voice: str,
        response_format: str,
        extra: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> GatewayRequest:
        """Build a text-to-speech request. JSON body ``{input, voice,
        response_format, model}``; uses the audio api-version. The deployment is
        carried in the path for Azure-native style, but the ``model`` field is
        ALSO required in the body: gpt-4o-mini-tts speech (on the 2025 audio
        api-version) rejects the request with ``missing_required_parameter``
        otherwise. openai-compatible style relies on ``model`` in the body."""
        path = self._speech_path.format(deployment=deployment)
        url = f"{self._base}{path if path.startswith('/') else '/' + path}"
        body: dict[str, Any] = {
            "input": text,
            "voice": voice,
            "response_format": response_format,
            "model": deployment,
            **(extra or {}),
        }
        if self._style == GatewayProviderStyle.azure_openai_native:
            url = f"{url}?api-version={self._audio_api_version}"
        return GatewayRequest(url=url, headers=self._auth_headers(correlation_id), json=body)

    async def synthesize_speech(
        self,
        *,
        deployment: str,
        text: str,
        voice: str,
        response_format: str,
        extra: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> bytes:
        """Synthesize speech; returns the raw audio bytes (mp3/wav/...). Uses the
        audio timeout (synthesis can take longer than a chat turn)."""
        req = self.build_speech_request(
            deployment=deployment,
            text=text,
            voice=voice,
            response_format=response_format,
            extra=extra,
            correlation_id=correlation_id,
        )
        if self._http is not None:
            client, owned = self._http, False
        else:
            client, owned = httpx.AsyncClient(timeout=self._audio_timeout), True
        try:
            resp = await client.post(
                req.url, headers=req.headers, json=req.json, timeout=self._audio_timeout
            )
            if resp.status_code >= 400:
                raise ModelGatewayError(resp.status_code, resp.text)
            return resp.content
        finally:
            if owned:
                await client.aclose()

    def transcription_url(self, deployment: str) -> str:
        """The transcription endpoint URL (path + audio api-version for native)."""
        path = self._transcription_path.format(deployment=deployment)
        url = f"{self._base}{path if path.startswith('/') else '/' + path}"
        if self._style == GatewayProviderStyle.azure_openai_native:
            url = f"{url}?api-version={self._audio_api_version}"
        return url

    async def transcribe(
        self,
        *,
        deployment: str,
        audio: bytes,
        filename: str,
        content_type: str,
        language: str | None = None,
        response_format: str = "json",
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Transcribe audio (speech-to-text). Sends multipart/form-data with the
        audio ``file`` part; returns the parsed provider JSON (``{text: ...}``).
        Openai-compatible style adds ``model`` as a form field."""
        url = self.transcription_url(deployment)
        data: dict[str, str] = {"response_format": response_format}
        if language:
            data["language"] = language
        if self._style != GatewayProviderStyle.azure_openai_native:
            data["model"] = deployment
        files = {"file": (filename, audio, content_type)}
        headers = self._auth_headers_multipart(correlation_id)
        if self._http is not None:
            client, owned = self._http, False
        else:
            client, owned = httpx.AsyncClient(timeout=self._audio_timeout), True
        try:
            resp = await client.post(
                url, headers=headers, data=data, files=files, timeout=self._audio_timeout
            )
            if resp.status_code >= 400:
                raise ModelGatewayError(resp.status_code, resp.text)
            # response_format=json -> JSON object. A non-JSON 200 means an upstream
            # misroute (e.g. an HTML error page); only trust plain text when text
            # was explicitly requested, otherwise treat it as a gateway failure.
            try:
                return resp.json()
            except ValueError:
                if response_format == "text":
                    return {"text": resp.text}
                raise ModelGatewayError(
                    502, "Unexpected non-JSON transcription response"
                ) from None
        finally:
            if owned:
                await client.aclose()

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
        api: str = "chat",
    ) -> dict[str, Any]:
        if api == "responses":
            req = self.build_responses_request(
                deployment=deployment,
                messages=messages,
                params=params,
                stream=False,
                correlation_id=correlation_id,
            )
        else:
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
            data = resp.json()
            if api == "responses":
                if data.get("status") == "failed":
                    err = (data.get("error") or {}).get("message") or "responses failed"
                    raise ModelGatewayError(502, err)
                return _responses_json_to_chat(data)
            return data
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
        api: str = "chat",
    ) -> AsyncIterator[ChatChunk]:
        if api == "responses":
            async for chunk in self._stream_responses(
                deployment=deployment,
                messages=messages,
                params=params,
                correlation_id=correlation_id,
            ):
                yield chunk
            return
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

    async def _stream_responses(
        self,
        *,
        deployment: str,
        messages: Sequence[dict[str, Any]],
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """Stream a Responses turn, translating its SSE events into the synthetic
        chat-shaped ``ChatChunk`` stream the router already consumes.

        Responses SSE frames are ``data:`` lines (Azure also emits ``event:``
        lines, which we ignore) terminated by a blank line, with NO ``[DONE]``
        sentinel — ``response.completed``/``response.incomplete`` are terminal and
        carry usage. We accumulate the (possibly multi-line) ``data:`` payload per
        frame before parsing.
        """
        client, owned = self._client()
        try:
            req = self.build_responses_request(
                deployment=deployment,
                messages=messages,
                params=params,
                stream=True,
                correlation_id=correlation_id,
            )
            async with client.stream(
                "POST", req.url, headers=req.headers, json=req.json
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise ModelGatewayError(
                        resp.status_code, body.decode("utf-8", "replace")
                    )
                data_buf: list[str] = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_buf.append(line[len("data:") :].lstrip())
                        continue
                    if line == "":
                        # Frame boundary: flush the accumulated data payload.
                        if data_buf:
                            chunk = _parse_responses_event("\n".join(data_buf))
                            data_buf = []
                            if chunk is not None:
                                yield chunk
                                if chunk.done:
                                    return
                    # ``event:``/comment lines carry no payload — ignore them.
                # Flush a trailing frame not followed by a blank line.
                if data_buf:
                    chunk = _parse_responses_event("\n".join(data_buf))
                    if chunk is not None:
                        yield chunk
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


def _parse_responses_event(payload: str) -> ChatChunk | None:
    """Translate one Responses SSE frame payload into a chat-shaped ChatChunk.

    Routes by the event's ``type``:
      * ``response.output_text.delta`` -> a text increment, mirrored into a
        chat-shaped ``raw`` so the existing frontend (which reads
        ``choices[].delta.content``) renders it unchanged.
      * ``response.completed`` / ``response.incomplete`` -> terminal; carry the
        mapped usage. ``incomplete`` (reasoning ran out the output budget) is NOT
        an error — the tokens were spent, so it must still meter + persist the
        partial answer.
      * ``response.failed`` -> raise so the router surfaces an error turn.
    Other event types (created/in_progress/reasoning/etc.) carry no user-visible
    delta and are dropped.
    """
    if not payload:
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    etype = obj.get("type")
    if etype == "response.output_text.delta":
        piece = obj.get("delta") or ""
        if not piece:
            return None
        return ChatChunk(
            delta=piece,
            raw=json.dumps({"choices": [{"delta": {"content": piece}}]}),
        )
    if etype in ("response.completed", "response.incomplete"):
        usage = _responses_usage_to_chat((obj.get("response") or {}).get("usage"))
        return ChatChunk(done=True, usage=usage)
    if etype == "response.failed":
        err = (((obj.get("response") or {}).get("error")) or {}).get("message")
        raise ModelGatewayError(502, err or "responses stream failed")
    return None
