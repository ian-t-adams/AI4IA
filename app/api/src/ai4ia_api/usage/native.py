"""Bounded native numeric evidence, never normalized meters or pricing inputs."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_NATIVE_USAGE_CALLS = 8
MAX_NATIVE_USAGE_VALUE = 2**53 - 1
NativeUsageApi = Literal["chat", "responses"]
NativeUsageField = Literal[
    "prompt_tokens", "completion_tokens", "total_tokens",
    "prompt_tokens_details.text_tokens", "prompt_tokens_details.cached_tokens",
    "prompt_tokens_details.audio_tokens", "prompt_tokens_details.image_tokens",
    "completion_tokens_details.reasoning_tokens", "completion_tokens_details.audio_tokens",
    "completion_tokens_details.accepted_prediction_tokens",
    "completion_tokens_details.rejected_prediction_tokens",
    "input_tokens", "output_tokens", "input_tokens_details.cached_tokens",
    "output_tokens_details.reasoning_tokens", "cost_in_usd_ticks", "cost_in_nano_usd",
]
_FIELDS: dict[NativeUsageApi, tuple[NativeUsageField, ...]] = {
    "chat": (
        "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_tokens_details.text_tokens", "prompt_tokens_details.cached_tokens",
        "prompt_tokens_details.audio_tokens", "prompt_tokens_details.image_tokens",
        "completion_tokens_details.reasoning_tokens", "completion_tokens_details.audio_tokens",
        "completion_tokens_details.accepted_prediction_tokens",
        "completion_tokens_details.rejected_prediction_tokens", "cost_in_usd_ticks",
    ),
    "responses": (
        "input_tokens", "output_tokens", "total_tokens", "input_tokens_details.cached_tokens",
        "output_tokens_details.reasoning_tokens", "cost_in_usd_ticks", "cost_in_nano_usd",
    ),
}
_Count = Annotated[int, Field(strict=True, ge=0, le=MAX_NATIVE_USAGE_VALUE)]


class NativeUsage(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True,
    )

    api: NativeUsageApi
    semantics: Literal["provider_reported"] = "provider_reported"
    status: Literal["reported", "partial", "invalid", "unreported"]
    values: dict[NativeUsageField, _Count] = Field(default_factory=dict, max_length=12)
    invalidFields: tuple[NativeUsageField, ...] = Field(default=(), max_length=12)

    @model_validator(mode="after")
    def validate_paths(self) -> NativeUsage:
        fields = set(self.values) | set(self.invalidFields)
        if (
            not fields.issubset(_FIELDS[self.api])
            or len(set(self.invalidFields)) != len(self.invalidFields)
            or set(self.values).intersection(self.invalidFields)
        ):
            raise ValueError("Native usage fields must match their API and observation state.")
        expected = (
            "partial" if self.values and self.invalidFields else "reported" if self.values
            else "invalid" if self.invalidFields else self.status
        )
        if self.status != expected or (not fields and self.status not in {"invalid", "unreported"}):
            raise ValueError("Native usage status does not match its retained fields.")
        return self


class NativeUsageBatch(BaseModel):
    """A prefix of individual calls, with absent historical observations kept absent."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    calls: tuple[NativeUsage | None, ...] = Field(max_length=MAX_NATIVE_USAGE_CALLS)
    totalCalls: int = Field(strict=True, ge=1, le=MAX_NATIVE_USAGE_VALUE)
    truncated: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_prefix(self) -> NativeUsageBatch:
        if (
            len(self.calls) != min(self.totalCalls, MAX_NATIVE_USAGE_CALLS)
            or self.truncated != (self.totalCalls > MAX_NATIVE_USAGE_CALLS)
        ):
            raise ValueError("Native usage must retain a counted, bounded call prefix.")
        return self


def _capture(raw: Any, api: NativeUsageApi) -> NativeUsage:
    if not isinstance(raw, dict):
        return NativeUsage(api=api, status="unreported" if raw is None else "invalid")
    values: dict[NativeUsageField, int] = {}
    invalid: list[NativeUsageField] = []
    for field in _FIELDS[api]:
        root, separator, child = field.partition(".")
        if root not in raw:
            continue
        value = raw[root]
        if separator:
            if not isinstance(value, dict):
                invalid.append(field)
                continue
            if child not in value:
                continue
            value = value[child]
        if type(value) is int and 0 <= value <= MAX_NATIVE_USAGE_VALUE:
            values[field] = value
        else:
            invalid.append(field)
    return NativeUsage(
        api=api, values=values, invalidFields=tuple(invalid),
        status="partial" if invalid and values else "invalid" if invalid
        else "reported" if values else "unreported",
    )


class GatewayUsage(dict[str, Any]):
    """Keep internal evidence off wire dictionaries and out of provider-controlled keys."""

    def __init__(self, values: dict[str, Any], native: NativeUsage) -> None:
        super().__init__(values)
        self.native = native


def capture_usage(
    raw: Any, *, api: NativeUsageApi, mapped: dict[str, Any] | None = None,
) -> GatewayUsage | None:
    if raw is None:
        return None
    values = mapped if mapped is not None else raw if isinstance(raw, dict) else {
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
    }
    return GatewayUsage(values, _capture(raw, api))


def merge_native_usage(
    left: NativeUsageBatch | None, left_calls: int,
    right: NativeUsageBatch | None, right_calls: int,
) -> NativeUsageBatch | None:
    if left is None and right is None:
        return None
    before = left.calls if left is not None else (None,) * min(left_calls, MAX_NATIVE_USAGE_CALLS)
    after = right.calls if right is not None else (None,) * min(right_calls, MAX_NATIVE_USAGE_CALLS)
    total = left_calls + right_calls
    return NativeUsageBatch(
        calls=(*before, *after)[:MAX_NATIVE_USAGE_CALLS],
        totalCalls=total, truncated=total > MAX_NATIVE_USAGE_CALLS,
    )
