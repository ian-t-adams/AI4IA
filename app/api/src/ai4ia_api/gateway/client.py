"""HTTP client that sends chat completions through the model gateway.

Request shaping is explicit (``provider_style``) so we never confuse the
Azure/Foundry-native shape (deployment in path + ``api-version`` query) with the
OpenAI-compatible shape (``model`` in body). URL construction is pure and
unit-tested; the exact APIM/proxy/Foundry path prefix is parameterized so it can
be fixed at integration time without code changes.
"""
from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import GatewayAuthMode, GatewayProviderStyle, Settings
from ..chat_timing import current_chat_timing
from ..http_retry import request_with_retry
from ..model_traits import (
    is_anthropic_messages_deployment,
    is_reasoning_deployment,
    supports_sampling,
)
from ..safety import MessageSafety, parse_safety
from .anthropic import (
    ANTHROPIC_API,
    AnthropicStreamState,
    anthropic_json_to_chat,
    build_anthropic_payload,
    parse_anthropic_event,
)
from .priority import PRIORITY_HEADER, get_request_priority

# Azure OpenAI reasoning models (the GPT-5 family and the o-series) reject the
# classic Chat Completions sampling/limit parameters. The predicate lives in
# ``model_traits`` because the catalog serializes the same answer to the web app;
# a second copy here would drift and the UI would start offering controls this
# module strips. See that module for the full rationale.
_is_reasoning_deployment = is_reasoning_deployment

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


# Request-body fields the gateway always builds itself. A caller-supplied value
# for any of these would replace the server's history, routing, prompt authority
# or retention posture, so they are stripped from ``params`` before the server
# value is written -- and the server value is written LAST, so ordering can never
# silently reintroduce the override.
#
# ``tools``/``tool_choice``/``parallel_tool_calls`` are deliberately NOT here:
# the agent runtime legitimately supplies a per-iteration tool schema through
# ``params`` (agents/runtime.py), and it already strips those same keys from
# *caller* params before doing so. External callers cannot reach these at all
# because the chat router validates ``params`` against a strict allowlist
# (routers/chat.py::ChatParams).
_SERVER_OWNED_BODY_KEYS = (
    "messages",
    "model",
    "input",
    "instructions",
    "include",
    "previous_response_id",
    "store",
    "stream",
    "stream_options",
)


def _without_server_owned(params: dict[str, Any] | None) -> dict[str, Any]:
    """Copy ``params`` without any field the gateway owns (see above)."""
    out = dict(params or {})
    for key in _SERVER_OWNED_BODY_KEYS:
        out.pop(key, None)
    return out


def _normalize_params_for_deployment(body: dict[str, Any], deployment: str) -> None:
    """In place: adapt chat params to a reasoning model's constraints.

    Translates ``max_tokens`` -> ``max_completion_tokens`` (preserving any
    caller-supplied ``max_completion_tokens``) and strips the sampling
    parameters that reasoning models reject. No-op for non-reasoning models, so
    gpt-4.1/4o and non-OpenAI deployments (e.g. DeepSeek) keep ``max_tokens``.
    """
    if not supports_sampling(deployment):
        for key in _REASONING_UNSUPPORTED_PARAMS:
            body.pop(key, None)
    if _is_reasoning_deployment(deployment):
        if "max_tokens" in body:
            value = body.pop("max_tokens")
            if value is not None:
                body.setdefault("max_completion_tokens", value)


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
RESPONSES_OUTPUT_ITEMS_KEY = "_responses_output_items"

# Reasoning models spend hidden "reasoning tokens" out of the same output budget
# as the visible answer, so a small ``max_output_tokens`` yields an EMPTY message
# with status ``incomplete``. The cap only *bounds* (never bills) unused tokens,
# so we floor it generously for Responses turns to keep short prompts from
# truncating to nothing; a caller asking for MORE is always honored.
_RESPONSES_MIN_OUTPUT_TOKENS = 16384


def _messages_to_responses_input(
    messages: Sequence[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Translate the internal chat/tool transcript to Responses input items.

    Every ``system`` message is concatenated (order-preserving) into the single
    Responses ``instructions`` field. Plain user/assistant turns remain messages;
    chat-style assistant ``tool_calls`` become flat ``function_call`` items and
    ``role:tool`` results become ``function_call_output`` items.

    During a live Responses tool loop, the runtime also carries the provider's
    original output items under :data:`RESPONSES_OUTPUT_ITEMS_KEY`. Replaying that
    array verbatim preserves item ordering and encrypted reasoning while
    ``store=false``; the chat-shaped fields exist only for the shared governance
    loop and are not duplicated into the provider request.
    """
    system_parts: list[str] = []
    items: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role == "system":
            if content:
                system_parts.append(content)
            continue

        provider_items = m.get(RESPONSES_OUTPUT_ITEMS_KEY)
        if isinstance(provider_items, list):
            items.extend(dict(item) for item in provider_items if isinstance(item, dict))
            continue

        if role == "tool":
            call_id = m.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            output = content if isinstance(content, str) else json.dumps(content, default=str)
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                }
            )
            continue

        tool_calls = m.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list):
            if content:
                items.append({"role": "assistant", "content": content})
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                call_id = call.get("id")
                if (
                    not isinstance(function, dict)
                    or not isinstance(call_id, str)
                    or not call_id
                ):
                    continue
                name = function.get("name")
                arguments = function.get("arguments")
                if not isinstance(name, str) or not name:
                    continue
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments if isinstance(arguments, str) else "{}",
                    }
                )
            continue

        if role in {"user", "assistant"}:
            items.append({"role": role, "content": content})
    instructions = "\n\n".join(system_parts) if system_parts else None
    return instructions, items


def _flatten_responses_tools(tools: Any) -> Any:
    """Convert nested Chat Completions function tools to Responses' flat shape."""
    if not isinstance(tools, list):
        return tools
    flattened: list[Any] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            flattened.append(tool)
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            flattened_function = dict(function)
            flattened_function["type"] = "function"
            flattened.append(flattened_function)
        else:
            # Already-flat Responses tools pass through unchanged.
            flattened.append(dict(tool))
    return flattened


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

    response_format = out.pop("response_format", None)
    if isinstance(response_format, dict):
        if (
            response_format.get("type") == "json_schema"
            and isinstance(response_format.get("json_schema"), dict)
        ):
            response_schema = dict(response_format["json_schema"])
            response_schema["type"] = "json_schema"
            out["text"] = {"format": response_schema}
        else:
            out["text"] = {"format": dict(response_format)}

    if "tools" in out:
        out["tools"] = _flatten_responses_tools(out["tools"])

    for key in _REASONING_UNSUPPORTED_PARAMS:
        out.pop(key, None)
    out.pop("stream", None)
    out.pop("stream_options", None)
    return out


def _responses_text(obj: dict[str, Any]) -> str:
    """Concatenate visible output text and refusal fragments."""
    parts: list[str] = []
    for item in obj.get("output") or []:
        if item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if block.get("type") == "output_text" and block.get("text"):
                parts.append(block["text"])
            elif block.get("type") == "refusal" and block.get("refusal"):
                parts.append(block["refusal"])
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
    output_items = [
        dict(item) for item in obj.get("output") or [] if isinstance(item, dict)
    ]
    tool_calls: list[dict[str, Any]] = []
    for item in output_items:
        if item.get("type") != "function_call":
            continue
        call_id = item.get("call_id")
        name = item.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            continue
        arguments = item.get("arguments")
        tool_calls.append(
            {
                # The shared runtime keys tool results by Chat Completions' ``id``.
                # Responses requires that same value as ``function_call_output.call_id``.
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments if isinstance(arguments, str) else "{}",
                },
            }
        )

    message: dict[str, Any] = {
        "role": "assistant",
        "content": _responses_text(obj) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    result: dict[str, Any] = {
        "choices": [{"message": message}],
        "_responses_status": obj.get("status"),
    }
    if obj.get("status") == "incomplete" and obj.get("incomplete_details") is not None:
        result["_responses_incomplete_reason"] = str(obj["incomplete_details"])[:200]
    if tool_calls:
        # Turn-local continuation state. The runtime replays it to Responses but
        # strips it from receipts and never persists it in the assistant message.
        result[RESPONSES_OUTPUT_ITEMS_KEY] = output_items
    usage = _responses_usage_to_chat(obj.get("usage"))
    if usage is not None:
        result["usage"] = usage
    if isinstance(obj.get("content_filters"), list):
        # Foundry's Responses extension uses a top-level content_filters array.
        # Keep it on the translated body so the shared safety normalizer sees the
        # same evidence as a direct Responses caller.
        result["content_filters"] = obj["content_filters"]
    return result


_REQUEST_FAILED = "Model gateway request failed."
_STREAM_FAILED = "Model stream failed."
MAI_API = "mai"
BFL_API = "bfl"
CHAT_COMPLETIONS_APIS = frozenset({"chat", MAI_API})
TOOL_CALLING_APIS = CHAT_COMPLETIONS_APIS | frozenset({"responses"})
_KNOWN_APIS = TOOL_CALLING_APIS | frozenset({ANTHROPIC_API})


def _resolved_api(api: str, deployment: str) -> str:
    """Resolve the provider protocol, retaining a safe agent-runtime fallback.

    Chat routers pass ``ModelEntry.api`` explicitly. Agent/workflow internals
    historically carry only the resolved deployment name, so Claude names are
    also recognized here; without this fallback an agent could select Claude in
    the UI and then send its turn to the wrong provider surface.
    """
    resolved = (
        ANTHROPIC_API
        if api == "chat" and is_anthropic_messages_deployment(deployment)
        else api
    )
    if resolved not in _KNOWN_APIS:
        raise ValueError(f"unsupported model API: {resolved}")
    return resolved


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
    # Annotate-only content-safety verdicts reported on this chunk, if any.
    # Azure reports prompt annotations early and completion annotations later in
    # the stream, so callers merge across chunks (``safety.merge_safety``).
    safety: MessageSafety | None = None
    incomplete: bool = False
    incompleteReason: str | None = None
    # Turn-local Responses continuation state. Never emitted to the browser.
    response_output_items: list[dict[str, Any]] | None = None


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
        self._apply_priority(headers)
        return headers

    def _apply_priority(self, headers: dict[str, str]) -> None:
        """Stamp the server-derived SimpleL7Proxy priority band, if any.

        The band comes from a ContextVar written only by the auth dependency —
        never copied from the inbound request — so a caller cannot promote itself
        into the reserved worker pool. When the feature is off the band is None
        and no header is sent, leaving the proxy on its own default.
        """
        band = get_request_priority()
        if band is not None:
            headers[PRIORITY_HEADER] = str(band)

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
        self._apply_priority(headers)
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

        # Caller params first, then every server-owned field, so a crafted
        # ``params`` can neither replace the server-built history nor smuggle in
        # an alternate deployment/stream posture.
        body: dict[str, Any] = {
            **_without_server_owned(params),
            "messages": list(messages),
        }
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

    def build_anthropic_request(
        self,
        *,
        deployment: str,
        messages: Sequence[dict[str, Any]],
        params: dict[str, Any] | None = None,
        stream: bool = False,
        correlation_id: str | None = None,
    ) -> GatewayRequest:
        """Build the internal gateway request for a Claude Messages turn.

        The proxy-facing path deliberately retains ``/deployments/{name}`` so
        SimpleL7Proxy can derive and stamp the server-owned model header. APIM
        then rewrites it to the provider's fixed ``/anthropic/v1/messages`` path,
        changes the Entra audience, and overrides ``anthropic-version``.
        """
        path = self._chat_path.format(deployment=deployment)
        url = f"{self._base}{path if path.startswith('/') else '/' + path}"
        return GatewayRequest(
            url=url,
            headers=self._auth_headers(correlation_id),
            json=build_anthropic_payload(
                deployment=deployment,
                messages=messages,
                params=params,
                stream=stream,
            ),
        )

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
            # Caller params FIRST so every server-owned field below wins. This
            # surface is the sharper of the two: ``model``, ``input`` and
            # ``store`` are all real Responses fields, so spreading caller params
            # last would let a caller re-target the deployment, replace the
            # server-built input, and switch provider retention back on.
            **_normalize_params_for_responses(_without_server_owned(params)),
            "model": deployment,
            "input": input_items,
            # Opt OUT of provider-side conversation storage. ``store`` defaults to
            # TRUE on this surface (unlike chat completions, which has no such
            # concept), so every Responses turn otherwise leaves a 30-day copy of
            # the user's prompt and the model's output retrievable from
            # ``GET /responses/{id}`` outside the app. Verified live, not assumed.
            # AI4IA keeps conversation state in Cosmos, scoped per user, and this
            # client re-sends the full history each turn rather than chaining with
            # ``previous_response_id`` -- so the retained copy buys nothing and is
            # purely a second, ungoverned store of user content.
            # If turn chaining is ever added, do NOT flip this back: request
            # ``include: ["reasoning.encrypted_content"]`` and pass the encrypted
            # reasoning items forward, which is the stateless-mode equivalent.
            "store": False,
        }
        if instructions:
            body["instructions"] = instructions
        if body.get("tools"):
            # Provider storage remains off. Encrypted reasoning is the documented
            # stateless continuation mechanism for reasoning-model tool loops.
            body["include"] = ["reasoning.encrypted_content"]
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
        api: str = "chat",
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
        if api == BFL_API:
            body = {
                "model": deployment.lower(),
                "prompt": prompt,
                "num_images": n,
                "output_format": "png",
                # Server-owned: callers cannot relax the provider safety posture.
                "safety_tolerance": 2,
            }
            if size:
                width, height = size.split("x", maxsplit=1)
                body["width"] = int(width)
                body["height"] = int(height)
        else:
            body = {"prompt": prompt, "n": n, **(extra or {})}
            if size:
                body["size"] = size
        if self._style == GatewayProviderStyle.azure_openai_native:
            url = f"{url}?api-version={self._image_api_version}"
        elif api != BFL_API:
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
        api: str = "chat",
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
            api=api,
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

    def build_document_ocr_request(
        self,
        *,
        deployment: str,
        data: bytes,
        content_type: str,
        correlation_id: str | None = None,
    ) -> GatewayRequest:
        """Build a Mistral document/OCR request through the governed gateway.

        The proxy-facing deployment path exists only so SimpleL7Proxy can stamp
        the server-owned model header. APIM rewrites it to the provider OCR path
        and binds the regional failover deployment in ``model``.

        The ``api-version`` below is inert: the generated APIM policy sets the
        Mistral provider version with ``exists-action="override"``
        (``infra/policies/simplel7proxy_backend_32.xml``), so the gateway owns it
        and whatever the client sends is discarded. Do not read this value as the
        version actually used, and do not "fix" a mismatch here — change the
        policy generator instead.
        """

        url = (
            f"{self._base}/deployments/{deployment}/document/ocr"
            f"?api-version={self._api_version}"
        )
        media_type = (content_type or "application/octet-stream").split(";", 1)[
            0
        ].strip()
        encoded = base64.b64encode(data).decode("ascii")
        is_image = media_type.startswith("image/")
        url_key = "image_url" if is_image else "document_url"
        body = {
            "model": deployment.lower(),
            "document": {
                "type": url_key,
                url_key: f"data:{media_type};base64,{encoded}",
            },
            "include_image_base64": False,
            "table_format": "markdown",
        }
        return GatewayRequest(
            url=url,
            headers=self._auth_headers(correlation_id),
            json=body,
        )

    async def analyze_document(
        self,
        *,
        deployment: str,
        data: bytes,
        content_type: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        req = self.build_document_ocr_request(
            deployment=deployment,
            data=data,
            content_type=content_type,
            correlation_id=correlation_id,
        )
        if self._http is not None:
            client, owned = self._http, False
        else:
            client, owned = httpx.AsyncClient(timeout=self._image_timeout), True
        try:
            response = await client.post(
                req.url,
                headers=req.headers,
                json=req.json,
                timeout=self._image_timeout,
            )
            if response.status_code >= 400:
                # The provider body can echo the base64 document back at us, so
                # only a bounded prefix is carried; callers reduce this further to
                # a status-only message before anything is logged or persisted.
                raise ModelGatewayError(
                    response.status_code, response.text[:200]
                )
            try:
                return response.json()
            except ValueError as exc:
                # A 2xx with a non-JSON body means the provider ran (and will bill)
                # but we cannot read the result. Surface it as a gateway error so
                # the Mistral client records a provider-completed failure instead of
                # letting a raw JSONDecodeError escape as an opaque ingest crash.
                raise ModelGatewayError(
                    502, "malformed provider document response"
                ) from exc
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
        resolved_api = _resolved_api(api, deployment)
        if resolved_api == "responses":
            req = self.build_responses_request(
                deployment=deployment,
                messages=messages,
                params=params,
                stream=False,
                correlation_id=correlation_id,
            )
        elif resolved_api == ANTHROPIC_API:
            req = self.build_anthropic_request(
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
        timing = current_chat_timing()
        timing_started = timing.gateway_started() if timing is not None else None
        try:
            try:
                resp = await client.post(req.url, headers=req.headers, json=req.json)
            except httpx.HTTPError as exc:
                raise ModelGatewayError(502, _REQUEST_FAILED) from exc
            if resp.status_code >= 400:
                raise ModelGatewayError(resp.status_code, resp.text)
            try:
                data = resp.json()
            except ValueError as exc:
                raise ModelGatewayError(502, _REQUEST_FAILED) from exc
            if not isinstance(data, dict):
                raise ModelGatewayError(502, _REQUEST_FAILED)
            if resolved_api == "responses":
                if data.get("status") == "failed":
                    err = (data.get("error") or {}).get("message") or "responses failed"
                    raise ModelGatewayError(502, err)
                return _responses_json_to_chat(data)
            if resolved_api == ANTHROPIC_API:
                if data.get("type") == "error":
                    err = (data.get("error") or {}).get("message")
                    raise ModelGatewayError(502, err or _REQUEST_FAILED)
                return anthropic_json_to_chat(data)
            return data
        finally:
            if timing is not None and timing_started is not None:
                timing.gateway_finished(timing_started)
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
    ) -> AsyncGenerator[ChatChunk, None]:
        timing = current_chat_timing()
        timing_started = timing.gateway_started() if timing is not None else None
        client: httpx.AsyncClient | None = None
        owned = False
        try:
            resolved_api = _resolved_api(api, deployment)
            if resolved_api == "responses":
                async with aclosing(
                    self._stream_responses(
                        deployment=deployment,
                        messages=messages,
                        params=params,
                        correlation_id=correlation_id,
                    )
                ) as response_stream:
                    async for chunk in response_stream:
                        yield chunk
                return
            if resolved_api == ANTHROPIC_API:
                async with aclosing(
                    self._stream_anthropic(
                        deployment=deployment,
                        messages=messages,
                        params=params,
                        correlation_id=correlation_id,
                    )
                ) as anthropic_stream:
                    async for chunk in anthropic_stream:
                        yield chunk
                return
            client, owned = self._client()
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
        except httpx.HTTPError as exc:
            raise ModelGatewayError(502, _STREAM_FAILED) from exc
        finally:
            if timing is not None and timing_started is not None:
                timing.gateway_finished(timing_started)
            if owned and client is not None:
                await client.aclose()

    async def _stream_anthropic(
        self,
        *,
        deployment: str,
        messages: Sequence[dict[str, Any]],
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> AsyncGenerator[ChatChunk, None]:
        """Translate Claude Messages SSE frames into chat-shaped chunks."""
        client, owned = self._client()
        state = AnthropicStreamState()
        try:
            req = self.build_anthropic_request(
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
                    if line == "" and data_buf:
                        event = parse_anthropic_event("\n".join(data_buf), state)
                        data_buf = []
                        if event is None:
                            continue
                        if event.error:
                            raise ModelGatewayError(502, _STREAM_FAILED)
                        chunk = ChatChunk(
                            delta=event.delta,
                            raw=event.raw,
                            usage=event.usage,
                            done=event.done,
                        )
                        yield chunk
                        if chunk.done:
                            return
                if data_buf:
                    event = parse_anthropic_event("\n".join(data_buf), state)
                    if event is not None:
                        if event.error:
                            raise ModelGatewayError(502, _STREAM_FAILED)
                        yield ChatChunk(
                            delta=event.delta,
                            raw=event.raw,
                            usage=event.usage,
                            done=event.done,
                        )
        except httpx.HTTPError as exc:
            raise ModelGatewayError(502, _STREAM_FAILED) from exc
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
    ) -> AsyncGenerator[ChatChunk, None]:
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
        except httpx.HTTPError as exc:
            raise ModelGatewayError(502, _STREAM_FAILED) from exc
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
    return ChatChunk(
        delta=delta, raw=payload, usage=obj.get("usage"), safety=parse_safety(obj)
    )


def _parse_responses_event(payload: str) -> ChatChunk | None:
    """Translate one Responses SSE frame payload into a chat-shaped ChatChunk.

    Routes by the event's ``type``:
      * ``response.output_text.delta`` -> a text increment, mirrored into a
        chat-shaped ``raw`` so the existing frontend (which reads
        ``choices[].delta.content``) renders it unchanged.
      * ``response.output_item.done`` -> capture the provider output item and, for
        a function call, mirror one complete chat-shaped tool-call fragment.
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
    if etype in {"response.output_text.delta", "response.refusal.delta"}:
        piece = obj.get("delta") or ""
        if not piece:
            return None
        return ChatChunk(
            delta=piece,
            raw=json.dumps({"choices": [{"delta": {"content": piece}}]}),
        )
    if etype == "response.output_item.done":
        item = obj.get("item")
        if not isinstance(item, dict):
            return None
        raw = ""
        if item.get("type") == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            if isinstance(call_id, str) and call_id and isinstance(name, str) and name:
                arguments = item.get("arguments")
                output_index = obj.get("output_index")
                index = (
                    output_index
                    if isinstance(output_index, int) and not isinstance(output_index, bool)
                    else 0
                )
                raw = json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": index,
                                            "id": call_id,
                                            "type": "function",
                                            "function": {
                                                "name": name,
                                                "arguments": (
                                                    arguments
                                                    if isinstance(arguments, str)
                                                    else "{}"
                                                ),
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
        return ChatChunk(raw=raw, response_output_items=[dict(item)])
    if etype in ("response.completed", "response.incomplete"):
        response = obj.get("response") or {}
        usage = _responses_usage_to_chat(response.get("usage"))
        raw_output = response.get("output")
        output_items = (
            [dict(item) for item in raw_output if isinstance(item, dict)]
            if isinstance(raw_output, list)
            else None
        )
        return ChatChunk(
            done=True,
            usage=usage,
            safety=parse_safety(response),
            incomplete=etype == "response.incomplete",
            incompleteReason=(
                str(response.get("incomplete_details") or "")[:200]
                if etype == "response.incomplete"
                else None
            ),
            # The terminal response is authoritative for ordering and replaces any
            # per-item snapshots collected while the stream was in progress.
            response_output_items=output_items,
        )
    if etype in {"response.failed", "error"}:
        err = (((obj.get("response") or {}).get("error")) or {}).get("message")
        if not err and isinstance(obj.get("error"), dict):
            err = obj["error"].get("message")
        raise ModelGatewayError(502, err or "responses stream failed")
    return None
