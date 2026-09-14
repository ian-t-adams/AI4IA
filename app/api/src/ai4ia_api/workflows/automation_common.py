"""Bounds and lossless identities for opt-in workflow automation."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..agents.approvals import arguments_digest
from ..agents.tools import redact_obj
from .models import MAX_STEPS

PROTOCOL_VERSION = 3
MAX_STATE_BYTES = 512 * 1024
TRANSITION_RESERVE_BYTES = 32 * 1024
MAX_ARGUMENT_BYTES = 8 * 1024
MAX_ACTIVE_RUNS = 4
MAX_SCHEDULES = 10
MAX_SCHEDULE_HISTORY = 20
MAX_APPROVALS_PER_STEP = 4
APPROVAL_SECONDS = 600
RECONCILE_SECONDS = 30
MISSED_GRACE_SECONDS = 300
MAX_RUN_DISPATCHES = 128
MAX_MODEL_CALLS = MAX_STEPS * 3
MAX_TOOL_CALLS = MAX_STEPS * 8
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class AutomationError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status: int = 409) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


class AutomationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_assignment=True)


class ExecutionLimits(AutomationModel):
    maxApplicationDispatches: int = Field(default=64, ge=1, le=MAX_RUN_DISPATCHES, strict=True)
    maxModelCalls: int = Field(default=MAX_MODEL_CALLS, ge=1, le=MAX_MODEL_CALLS, strict=True)
    maxToolCalls: int = Field(default=MAX_TOOL_CALLS, ge=0, le=MAX_TOOL_CALLS, strict=True)
    maxOutputTokens: int = Field(default=1024, ge=1, le=32768, strict=True)
    maxRuntimeSeconds: int = Field(default=1800, ge=1, le=86400, strict=True)
    spendMode: str
    maxSpendMicroUsd: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def explicit_unsupported_spend(self) -> ExecutionLimits:
        if self.spendMode != "no_hard_dollar_cap" or self.maxSpendMicroUsd is not None:
            raise ValueError(
                "A finite dollar cap is unsupported by the current gateway attempt envelope. "
                "Select no_hard_dollar_cap explicitly; request/runtime limits are not a bill cap."
            )
        return self


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AutomationError("invalid_time", "An explicit timezone is required.", status=422)
    return value.astimezone(timezone.utc)


def json_bytes(value: Any, *, limit: int = MAX_STATE_BYTES) -> bytes:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise AutomationError("invalid_json", "Workflow state must be finite JSON.", status=422) from exc
    if len(encoded) > limit:
        raise AutomationError("state_limit", "Workflow state exceeds its durable size limit.")
    return encoded


def digest(value: Any) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def stable_id(owner: str, *parts: str) -> str:
    return hashlib.sha256(json_bytes([owner, *parts])).hexdigest()


def request_time(key: str) -> datetime:
    if len(key) > 128 or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z~[0-9a-f-]{32,36}", key,
    ) is None:
        raise AutomationError(
            "invalid_idempotency_key", "A timestamped workflow invocation key is required.", status=422,
        )
    try:
        return utc(datetime.fromisoformat(key.split("~", 1)[0].replace("Z", "+00:00")))
    except ValueError as exc:
        raise AutomationError("invalid_idempotency_key", "The invocation time is invalid.", status=422) from exc


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate argument key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite argument")


def exact_arguments(
    raw: str, *, visible_resource_ids: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], str, str]:
    """Refuse an executable call that cannot be represented completely and safely."""
    if len(raw.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise AutomationError("arguments_not_reviewable", "The complete call is too large to review.")
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise AutomationError("arguments_not_reviewable", "The call is not unambiguous JSON.") from exc
    if not isinstance(parsed, dict):
        raise AutomationError("arguments_not_reviewable", "Tool arguments must be a JSON object.")
    pending: list[tuple[Any, int]] = [(parsed, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > 1024 or depth > 16:
            raise AutomationError("arguments_not_reviewable", "The complete call is too complex to review.")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    encoded = json_bytes(parsed, limit=MAX_ARGUMENT_BYTES)
    redacted = redact_obj(parsed)
    if (
        isinstance(parsed.get("document_id"), str)
        and parsed["document_id"] in visible_resource_ids
    ):
        # These are canonical, ownership-checked resource IDs, not caller-supplied
        # credential labels. The full ID remains visible in the deciding screen.
        redacted["document_id"] = parsed["document_id"]
    if redacted != parsed:
        raise AutomationError(
            "arguments_not_reviewable",
            "Credential redaction would hide execution-significant arguments. This call cannot be approved.",
        )
    return parsed, arguments_digest(parsed), encoded.decode("ascii")
