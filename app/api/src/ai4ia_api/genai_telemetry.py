"""Content-free GenAI spans on the existing, connection-gated OTel exporter."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import httpx

from . import logging_setup
from .catalog import load_catalog
from .config import Settings
from .model_evidence import _parameters

if TYPE_CHECKING:
    from opentelemetry.trace import Span

INSTRUMENTATION_NAME = "ai4ia_api.genai"
CONTRACT_VERSION = "1.0.0"
# GenAI moved out of semantic-conventions after v1.43. This development snapshot
# has no published schema URL; do not label it as a released/stable OTel schema.
SEMCONV_REVISION = "0c87594975195608dc91b3f702e250a7b240c151"
_MAX_COUNT = 2**53 - 1
_FINISH_REASONS = frozenset({
    "stop", "length", "tool_calls", "function_call", "content_filter", "end_turn",
    "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal",
    "completed", "incomplete", "max_output_tokens",
})
ATTRIBUTE_NAMES = frozenset({
    "gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.system", "gen_ai.request.model",
    "gen_ai.request.stream", "gen_ai.request.max_tokens", "gen_ai.request.temperature",
    "gen_ai.request.top_p", "gen_ai.request.reasoning.level", "gen_ai.response.model",
    "gen_ai.response.finish_reasons", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "ai4ia.gen_ai.http_attempts", "ai4ia.gen_ai.usage.coverage",
    "ai4ia.gen_ai.semconv_revision", "error.type",
})


class ModelTelemetry:
    def __init__(self, settings: Settings) -> None:
        self.enabled = bool(settings.applicationinsights_connection_string)
        self.models: dict[str, frozenset[str]] = {}
        if self.enabled:
            catalog = load_catalog(
                settings.model_catalog_path, settings.data_residency, settings.claude_enabled,
            )
            for entry in catalog.models:
                for option in entry.options:
                    self.models[option.deploymentName] = frozenset(
                        name for name in (entry.id, option.deploymentName)
                        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", name)
                    )

    def start(self, deployment: str, api: str) -> ModelSpan:
        return ModelSpan(
            enabled=self.enabled and logging_setup.telemetry_enabled(),
            deployment=deployment, api=api, models=self.models.get(deployment, frozenset()),
        )


class ModelSpan:
    def __init__(
        self, *, enabled: bool, deployment: str, api: str, models: frozenset[str],
    ) -> None:
        self.span: Span | None = None
        self.models = models
        self.api = api
        self.attempts = 0
        self.completed = False
        self.failed = False
        self.counts: tuple[int, int] | None = None
        self.response_model: str | None = None
        self.model_conflict = False
        self.reasons: dict[int, str] = {}
        self.request_attributes: dict[str, str | int | float | bool] = {}
        if not enabled:
            return
        from opentelemetry import trace
        from opentelemetry.instrumentation.utils import is_instrumentation_enabled

        if not is_instrumentation_enabled():
            return
        operation = "generate_content" if api == "responses" else "chat"
        provider = "anthropic" if api == "anthropic" else "openai" if api != "mai" else "azure.ai.inference"
        attributes: dict[str, str] = {
            "gen_ai.operation.name": operation,
            "gen_ai.provider.name": provider,
            # The pinned Azure exporter still classifies dependencies by this
            # deprecated alias. Keep it equal to provider.name, never content.
            "gen_ai.system": provider,
            "ai4ia.gen_ai.semconv_revision": SEMCONV_REVISION,
        }
        # Only a catalog-owned identifier may enter a span name or model field.
        # Provider-returned arbitrary strings are not made safe by a regex.
        model = deployment if deployment in models else None
        if model is not None:
            attributes["gen_ai.request.model"] = model
        self.span = trace.get_tracer(
            INSTRUMENTATION_NAME, CONTRACT_VERSION,
        ).start_span(
            f"{operation} {model}" if model else operation,
            kind=trace.SpanKind.CLIENT, attributes=attributes,
        )

    @contextmanager
    def http_scope(self) -> Iterator[None]:
        # Never keep an OTel ContextVar token across an async-generator yield:
        # ASGI disconnect cleanup can close it in a different task.
        if not logging_setup.telemetry_enabled():
            yield
            return
        from opentelemetry.instrumentation.utils import suppress_http_instrumentation

        with suppress_http_instrumentation():
            yield

    def request(self, body: dict[str, Any]) -> None:
        if self.span is None:
            return
        self.attempts += 1
        parameters, _ = _parameters(body)
        self.request_attributes = {}
        for name, value in (
            ("gen_ai.request.max_tokens", parameters.maxOutputTokens),
            ("gen_ai.request.temperature", parameters.temperature),
            ("gen_ai.request.top_p", parameters.topP),
            ("gen_ai.request.reasoning.level", parameters.reasoningEffort),
        ):
            if value is not None:
                self.request_attributes[name] = value
        self.request_attributes["gen_ai.request.stream"] = body.get("stream") is True

    def response_metadata(self, body: dict[str, Any]) -> None:
        if self.span is None:
            return
        response = body
        if self.api == "responses" and isinstance(body.get("response"), dict):
            response = body["response"]
        elif self.api == "anthropic" and isinstance(body.get("message"), dict):
            response = body["message"]
        if isinstance(response.get("error"), dict) and response["error"]:
            self._fail("provider")
        model = response.get("model")
        if model is not None:
            if not isinstance(model, str) or model not in self.models:
                self.model_conflict = True
            elif self.response_model is not None and model != self.response_model:
                self.model_conflict = True
            else:
                self.response_model = model
        if self.api == "responses":
            details = response.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else response.get("status")
            self._reason(0, reason)
        elif self.api == "anthropic":
            delta = body.get("delta")
            self._reason(0, delta.get("stop_reason") if isinstance(delta, dict) else response.get("stop_reason"))
        else:
            choices = body.get("choices")
            if isinstance(choices, list) and len(choices) <= 8:
                for index, choice in enumerate(choices):
                    if isinstance(choice, dict):
                        self._reason(choice.get("index", index), choice.get("finish_reason"))

    def _reason(self, index: object, value: object) -> None:
        if (
            isinstance(index, int) and not isinstance(index, bool) and 0 <= index < 8
            and isinstance(value, str) and value in _FINISH_REASONS
        ):
            self.reasons[index] = value

    def usage(self, raw: object, *, completed: bool) -> None:
        if self.span is None:
            return
        self.completed = self.completed or completed
        if raw is None:
            return
        # Stream usage is cumulative: replace, never sum repeated reports.
        self.counts = None
        if isinstance(raw, dict) and all(
            type(raw.get(key)) is int and 0 <= raw[key] <= _MAX_COUNT
            for key in ("prompt_tokens", "completion_tokens")
        ):
            self.counts = (raw["prompt_tokens"], raw["completion_tokens"])

    def error(self, exc: BaseException) -> None:
        if self.span is None or self.failed:
            return
        if self.completed and isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            # Consumers normally close on the terminal chunk instead of asking
            # for another item. The provider already completed in that case.
            return
        if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            category = "cancelled"
        elif isinstance(exc, httpx.TimeoutException) or isinstance(exc.__cause__, httpx.TimeoutException):
            category = "timeout"
        elif isinstance(exc, httpx.HTTPError) or isinstance(exc.__cause__, httpx.HTTPError):
            category = "transport"
        elif type(getattr(exc, "status_code", None)) is int:
            code = getattr(exc, "status_code")
            category = (
                "authentication" if code in (401, 403) else "rate_limit" if code == 429
                else "provider" if code >= 500 else "request"
            )
        else:
            category = "execution"
        self._fail(category)

    def _fail(self, category: str) -> None:
        from opentelemetry.trace import Status, StatusCode

        if self.span is not None:
            self.failed = True
            self.span.set_attribute("error.type", category)
            self.span.set_status(Status(StatusCode.ERROR))

    def finish(self) -> None:
        if self.span is None:
            return
        if not self.completed and not self.failed:
            self._fail("incomplete_stream")
        known = self.counts is not None and self.completed and not self.failed
        for name, value in self.request_attributes.items():
            self.span.set_attribute(name, value)
        self.span.set_attribute("ai4ia.gen_ai.http_attempts", self.attempts)
        self.span.set_attribute("ai4ia.gen_ai.usage.coverage", "known" if known else "unknown")
        if known and self.counts is not None:
            self.span.set_attribute("gen_ai.usage.input_tokens", self.counts[0])
            self.span.set_attribute("gen_ai.usage.output_tokens", self.counts[1])
        if self.response_model is not None and not self.model_conflict:
            self.span.set_attribute("gen_ai.response.model", self.response_model)
        if self.reasons:
            self.span.set_attribute(
                "gen_ai.response.finish_reasons",
                tuple(self.reasons[index] for index in sorted(self.reasons)),
            )
        # No events, links, exception recording, status description, ambient
        # baggage, or content attributes. OTel timestamps provide duration.
        self.span.end()
        self.span = None
