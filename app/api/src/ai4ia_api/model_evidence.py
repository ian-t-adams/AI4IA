"""Bounded application request evidence, observed only at the gateway boundary.

The scope is one model call, not a request-wide ambient logger. Tool handlers,
background memory work and linked agents cannot inherit a parent's parameters.
No prompt, headers, URL, provider exception or opaque continuation enters here.
"""
from __future__ import annotations

import math
import re
from collections.abc import AsyncGenerator, Awaitable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agents.tools import redact
from .hard_quota.models import AdmissionEvidence
from .usage.models import TokenUsage
from .usage.pricing import PricingBook

MAX_RECORDED_MODEL_CALLS = 8
MAX_PRICE_VERSIONS = 16
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_PRICE_RATE = 1_000_000_000
ParameterName = Literal["temperature", "top_p", "max_tokens", "reasoning_effort"]
PARAMETER_NAMES: tuple[ParameterName, ...] = (
    "temperature", "top_p", "max_tokens", "reasoning_effort",
)
ParameterSource = Literal["request", "application_default", "workflow_default", "delegation_default"]
ModelSource = Literal["request", "session", "agent", "workflow", "supervisor", "unknown"]
ModelApi = Literal["chat", "responses", "anthropic", "unknown"]
Coverage = Literal["known", "partial", "unknown"]
T = TypeVar("T")


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EffectiveModelParameters(_Evidence):
    """Only safe scalar controls, after application/provider-adapter adaptation."""

    temperature: float | None = Field(default=None, ge=0, le=2, strict=True)
    topP: float | None = Field(default=None, ge=0, le=1, strict=True)
    maxOutputTokens: int | None = Field(default=None, ge=1, le=2**31 - 1, strict=True)
    outputTokenField: Literal["max_tokens", "max_completion_tokens", "max_output_tokens"] | None = None
    reasoningEffort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    toolChoice: Literal["auto", "none", "required", "any", "named"] | None = None
    parallelToolCalls: bool | None = Field(default=None, strict=True)


class ModelCostEstimate(_Evidence):
    coverage: Literal["known", "unknown"] = "unknown"
    estCostMicroUsd: int | None = Field(default=None, ge=0, le=_MAX_SAFE_INTEGER, strict=True)
    currency: Literal["USD"] = "USD"
    pricingBasis: Literal["input_output_tokens"] = "input_output_tokens"
    priceVersion: str | None = Field(default=None, max_length=128)
    priceInputPer1M: float | None = Field(default=None, ge=0, le=_MAX_PRICE_RATE, strict=True)
    priceOutputPer1M: float | None = Field(default=None, ge=0, le=_MAX_PRICE_RATE, strict=True)


class ReceiptCostSummary(_Evidence):
    """A known estimate or a known subtotal; never a claim about all service charges."""

    coverage: Coverage = "unknown"
    estCostMicroUsd: int | None = Field(default=None, ge=0, le=_MAX_SAFE_INTEGER, strict=True)
    currency: Literal["USD"] = "USD"
    pricingBasis: Literal["model_tokens_only"] = "model_tokens_only"
    totalCalls: int = Field(default=0, ge=0)
    pricedCalls: int = Field(default=0, ge=0)
    priceVersions: tuple[str, ...] = Field(default=(), max_length=MAX_PRICE_VERSIONS)
    priceVersionsTruncated: bool = False


class ModelCallEvidence(_Evidence):
    iteration: int = Field(ge=1)
    scope: Literal["application_effective"] = "application_effective"
    providerInternals: Literal["unknown"] = "unknown"
    modelId: str | None = Field(default=None, max_length=128)
    api: ModelApi = "unknown"
    modelSource: ModelSource = "unknown"
    parameterSource: ParameterSource = "application_default"
    requestOverrides: tuple[ParameterName, ...] = Field(default=(), max_length=4)
    coverage: Literal["recorded", "partial", "unknown"] = "unknown"
    parameters: EffectiveModelParameters | None = None
    httpAttempts: int = Field(default=0, ge=0)
    providerCompleted: bool = False
    usageKnown: bool = False
    usageComplete: bool = False
    promptTokens: int | None = Field(default=None, ge=0, le=_MAX_SAFE_INTEGER, strict=True)
    completionTokens: int | None = Field(default=None, ge=0, le=_MAX_SAFE_INTEGER, strict=True)
    cost: ModelCostEstimate = Field(default_factory=ModelCostEstimate)
    admissions: tuple[AdmissionEvidence, ...] = Field(default=(), max_length=2)


def _identifier(value: str | None) -> str | None:
    return value if (
        value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value)
        and redact(value) == value
    ) else None


def _parameters(body: dict[str, Any]) -> tuple[EffectiveModelParameters, bool]:
    values: dict[str, Any] = {}
    for source, target in (
        ("temperature", "temperature"), ("top_p", "topP"),
        ("reasoning_effort", "reasoningEffort"), ("parallel_tool_calls", "parallelToolCalls"),
    ):
        if source in body:
            values[target] = body[source]
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        if key in body:
            values["maxOutputTokens"] = body[key]
            values["outputTokenField"] = key
            break
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and "effort" in reasoning:
        values["reasoningEffort"] = reasoning["effort"]
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        kind = choice.get("type")
        values["toolChoice"] = "named" if kind in ("tool", "function") else kind
        if "disable_parallel_tool_use" in choice:
            disabled = choice["disable_parallel_tool_use"]
            values["parallelToolCalls"] = not disabled if isinstance(disabled, bool) else disabled
    elif choice is not None:
        values["toolChoice"] = choice
    try:
        return EffectiveModelParameters.model_validate(values), True
    except ValidationError as exc:
        # Invalid allowlisted values are evidence gaps, not strings to redact and
        # retain. Neither their bytes nor the validation messages are persisted.
        for error in exc.errors(include_input=False):
            key = error["loc"][0]
            if isinstance(key, str):
                values.pop(key, None)
        if "maxOutputTokens" not in values:
            values.pop("outputTokenField", None)
        return EffectiveModelParameters.model_validate(values), False


def _usage(raw: dict[str, Any] | None) -> TokenUsage:
    # TokenUsage's legacy parser accepts totals-only reports. Those cannot price
    # input/output independently, so they must not turn into a free estimate.
    if not isinstance(raw, dict) or not all(
        isinstance(raw.get(key), int)
        and not isinstance(raw[key], bool)
        and 0 <= raw[key] <= _MAX_SAFE_INTEGER
        for key in ("prompt_tokens", "completion_tokens")
    ):
        return TokenUsage.parse(None)
    return TokenUsage.parse(raw)


def _api(value: str) -> ModelApi:
    if value == "chat":
        return "chat"
    if value == "responses":
        return "responses"
    if value == "anthropic":
        return "anthropic"
    return "unknown"


def combine_costs(
    costs: Sequence[ReceiptCostSummary], *, expected_calls: int = 0,
) -> ReceiptCostSummary:
    total = max(expected_calls, sum(cost.totalCalls for cost in costs))
    priced = sum(cost.pricedCalls for cost in costs)
    subtotal = sum(cost.estCostMicroUsd or 0 for cost in costs)
    versions = sorted({version for cost in costs for version in cost.priceVersions})
    if subtotal > _MAX_SAFE_INTEGER:
        priced = 0
    return ReceiptCostSummary(
        coverage="known" if total and priced == total else "partial" if priced else "unknown",
        estCostMicroUsd=subtotal if priced else None,
        totalCalls=total, pricedCalls=priced,
        priceVersions=tuple(versions[:MAX_PRICE_VERSIONS]),
        priceVersionsTruncated=(
            len(versions) > MAX_PRICE_VERSIONS
            or any(cost.priceVersionsTruncated for cost in costs)
        ),
    )


@dataclass
class CapturedModelCall:
    iteration: int
    model_id: str | None
    api: str
    model_source: ModelSource
    parameter_source: ParameterSource
    overrides: tuple[ParameterName, ...]
    pricing: PricingBook
    parameters: EffectiveModelParameters | None = None
    parameters_valid: bool = False
    attempts: int = 0
    completed: bool = False
    usage: TokenUsage = field(default_factory=lambda: TokenUsage.parse(None))
    admissions: list[AdmissionEvidence] = field(default_factory=list)

    def report_admission(self, value: AdmissionEvidence) -> None:
        for index, previous in enumerate(self.admissions):
            if previous.operationHash == value.operationHash:
                self.admissions[index] = value
                return
        if len(self.admissions) < 2:
            self.admissions.append(value)

    def request(self, body: dict[str, Any]) -> None:
        self.parameters, self.parameters_valid = _parameters(body)
        self.attempts += 1

    def report_usage(self, raw: dict[str, Any] | None, *, completed: bool = False) -> None:
        if raw is not None:
            self.usage = _usage(raw)
        self.completed = self.completed or completed

    def snapshot(self) -> ModelCallEvidence:
        rate = self.pricing.rate(self.model_id or "")
        rates_valid = self.pricing.currency == "USD" and (
            rate is None or all(
                math.isfinite(value) and 0 <= value <= _MAX_PRICE_RATE
                for value in (rate.input_per_1m, rate.output_per_1m)
            )
        )
        usable_usage = self.usage.known and self.completed and rates_valid
        estimate = self.pricing.estimate(
            self.model_id or "",
            prompt_tokens=self.usage.prompt if usable_usage else None,
            completion_tokens=self.usage.completion if usable_usage else None,
        )
        known = (
            estimate.known and rates_valid and estimate.currency == "USD"
            and estimate.micro_usd is not None and 0 <= estimate.micro_usd <= _MAX_SAFE_INTEGER
        )
        return ModelCallEvidence(
            iteration=self.iteration, modelId=_identifier(self.model_id),
            api=_api(self.api),
            modelSource=self.model_source, parameterSource=self.parameter_source,
            requestOverrides=self.overrides,
            coverage=(
                "recorded" if self.parameters_valid else
                "partial" if self.parameters is not None else "unknown"
            ),
            parameters=self.parameters, httpAttempts=self.attempts,
            admissions=tuple(self.admissions),
            providerCompleted=self.completed,
            usageKnown=self.usage.known, usageComplete=self.usage.known and self.usage.complete,
            promptTokens=self.usage.prompt if self.usage.known else None,
            completionTokens=self.usage.completion if self.usage.known else None,
            cost=ModelCostEstimate(
                coverage="known" if known else "unknown",
                estCostMicroUsd=estimate.micro_usd if known else None,
                priceVersion=_identifier(estimate.version),
                priceInputPer1M=estimate.input_per_1m if rates_valid else None,
                priceOutputPer1M=estimate.output_per_1m if rates_valid else None,
            ),
        )


_current_recorder: ContextVar[ModelCallRecorder | None] = ContextVar(
    "model_call_evidence", default=None,
)


class ModelCallRecorder:
    def __init__(
        self, *, model_id: str | None = None, deployment: str | None = None,
        pricing: PricingBook | None = None, model_source: ModelSource = "unknown",
        parameter_source: ParameterSource = "application_default",
        overrides: tuple[ParameterName, ...] = (),
    ) -> None:
        self.model_id = model_id
        self.deployment = deployment
        self.pricing = pricing or PricingBook({}, currency="USD", version=None)
        self.model_source: ModelSource = model_source
        self.parameter_source: ParameterSource = parameter_source
        self.overrides = overrides
        self.count = 0
        self._calls: list[CapturedModelCall] = []

    @contextmanager
    def bind(self) -> Iterator[None]:
        token = _current_recorder.set(self)
        try:
            yield
        finally:
            _current_recorder.reset(token)

    async def observe(self, operation: Awaitable[T]) -> T:
        with self.bind():
            return await operation

    async def observe_stream(self, stream: AsyncGenerator[T, None]) -> AsyncGenerator[T, None]:
        # Do not hold a ContextVar token across a yield to an ASGI consumer:
        # disconnect cleanup can close the generator in a different task.
        try:
            while True:
                with self.bind():
                    try:
                        item = await anext(stream)
                    except StopAsyncIteration:
                        return
                yield item
        finally:
            with self.bind():
                await stream.aclose()

    def start(self, deployment: str, api: str) -> CapturedModelCall:
        self.count += 1
        model_id = self.model_id if deployment == self.deployment else None
        call = CapturedModelCall(
            iteration=self.count, model_id=model_id, api=api,
            model_source=self.model_source, parameter_source=self.parameter_source,
            overrides=self.overrides,
            pricing=self.pricing.snapshot_token_prices(model_id),
        )
        if len(self._calls) < MAX_RECORDED_MODEL_CALLS:
            self._calls.append(call)
        return call

    def snapshot(self) -> list[ModelCallEvidence]:
        return [call.snapshot() for call in self._calls]

    def cost(self) -> ReceiptCostSummary:
        return combine_costs([
            ReceiptCostSummary(
                coverage=call.cost.coverage,
                estCostMicroUsd=call.cost.estCostMicroUsd, totalCalls=1,
                pricedCalls=int(call.cost.coverage == "known"),
                priceVersions=(call.cost.priceVersion,) if call.cost.priceVersion else (),
            )
            for call in self.snapshot()
        ], expected_calls=self.count)


def begin_model_call(deployment: str, api: str) -> CapturedModelCall | None:
    recorder = _current_recorder.get()
    return recorder.start(deployment, api) if recorder is not None else None
