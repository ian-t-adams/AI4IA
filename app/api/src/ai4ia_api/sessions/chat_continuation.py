"""Private execution state, never a transcript or receipt projection."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..agents.turn_checkpoint import TurnCheckpoint

CHAT_HEAD_ID = "__ai4ia_chat_turn_v1"
CHAT_HEAD_KIND = "chat_turn_head_v1"
MAX_CHAT_STATE_BYTES = 512 * 1024
CHAT_TRANSITION_RESERVE_BYTES = 32 * 1024
CHAT_APPROVAL_SECONDS = 600


def is_chat_head(raw: dict[str, Any], user_id: str, session_id: str) -> bool:
    fields = {key for key in raw if not key.startswith("_")}
    return (
        fields == {"id", "kind", "userId", "sessionId", "turnId", "assistantId"}
        and raw.get("id") == CHAT_HEAD_ID and raw.get("kind") == CHAT_HEAD_KIND
        and raw.get("userId") == user_id and raw.get("sessionId") == session_id
        and all(
            value is None or (
                isinstance(value, str) and 0 < len(value) <= 128
                and not value.startswith("__ai4ia_")
            ) for value in (raw.get("turnId"), raw.get("assistantId"))
        )
        and (raw.get("assistantId") is None or raw.get("turnId") is not None)
    )


class ChatContinuationError(RuntimeError):
    def __init__(self, detail: str, *, status: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


class ChatContinuationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)
    request: dict[str, Any] = Field(repr=False)
    seed: dict[str, Any] = Field(repr=False)
    turn: TurnCheckpoint | None = Field(default=None, repr=False)
    children: dict[str, TurnCheckpoint] = Field(default_factory=dict, max_length=2, repr=False)
    childBindings: dict[str, str] = Field(default_factory=dict, max_length=2)


class ChatContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)
    version: Literal[1] = 1
    turnId: str
    revision: int = Field(ge=0)
    status: Literal["running", "waiting", "dispatched", "complete", "cancelled", "expired", "unknown"]
    expiresAt: datetime
    payload: ChatContinuationPayload | None = Field(repr=False)
    operationId: str | None = None
    reason: str | None = None

    @classmethod
    def from_persisted(cls, value: Any) -> ChatContinuation:
        if not isinstance(value, dict) or not set(cls.model_fields).issubset(value):
            raise ChatContinuationError("The private continuation record is incomplete.")
        payload = value["payload"]
        if payload is not None:
            if not isinstance(payload, dict) or not set(ChatContinuationPayload.model_fields).issubset(payload):
                raise ChatContinuationError("The private execution payload is incomplete.")
            if payload["turn"] is not None:
                TurnCheckpoint.from_persisted(payload["turn"])
        return cls.model_validate(value)

    def cleared(self, status: Literal["complete", "cancelled", "expired", "unknown"], reason: str) -> ChatContinuation:
        return self.model_copy(update={
            "revision": self.revision + 1, "status": status, "payload": None, "reason": reason,
        }, deep=True)

    def bounded_body(self) -> dict[str, Any]:
        body = self.model_dump(mode="json")
        try:
            encoded = json.dumps(body, ensure_ascii=True, allow_nan=False).encode("ascii")
        except (ValueError, RecursionError) as exc:
            raise ChatContinuationError("This pending call cannot be stored safely.") from exc
        if len(encoded) > MAX_CHAT_STATE_BYTES - CHAT_TRANSITION_RESERVE_BYTES:
            raise ChatContinuationError("This turn is too large to retain for exact-call approval.")
        return body


def validate_chat_transition(prior: ChatContinuation | None, updated: ChatContinuation) -> None:
    if prior is None:
        if updated.revision != 0 or updated.status != "running":
            raise ChatContinuationError("A new turn must start without dispatch authority.")
        return
    if (
        updated.turnId != prior.turnId or updated.revision != prior.revision + 1
        or (
            updated.expiresAt != prior.expiresAt
            and not (updated.status == "waiting" and updated.operationId != prior.operationId)
        )
        or (prior.payload is None and updated.payload is not None)
        or (
            prior.status in {"complete", "cancelled", "expired", "unknown"}
            and updated.status != prior.status
        )
        or (
            prior.payload is not None and updated.payload is not None
            and prior.payload.request != updated.payload.request
        )
    ):
        raise ChatContinuationError("A checkpoint cannot restore or change the original turn's authority.")


_AUTH_KEYS = frozenset({
    "authorization", "proxy-authorization", "api-key", "apikey", "password", "passwd",
    "secret", "client-secret", "access-token", "refresh-token", "credential",
    "ocp-apim-subscription-key", "subscription-key", "s7p-key",
})
_AUTH_TEXT = re.compile(
    r"(?i)(?:^\s*Bearer\s+\S{16,}\s*$|\bAuthorization\s*:\s*(?:Bearer|Basic)\s+\S+"
    r"|https?://[^/\s@]+@|[?&](?:sig|signature|access_token|client_secret)=\S+)"
)


def exact_chat_arguments(raw: str) -> dict[str, Any]:
    """Preserve execution data; authentication belongs in server-side resolvers."""
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError("nonfinite JSON")

    try:
        if len(raw.encode("utf-8")) > 64 * 1024:
            raise ValueError("argument limit")
        parsed = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
        if not isinstance(parsed, dict):
            raise ValueError("object required")
        pending: list[tuple[Any, int]] = [(parsed, 0)]
        count = 0
        while pending:
            value, depth = pending.pop()
            count += 1
            if count > 8192 or depth > 24:
                raise ValueError("complexity limit")
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.lower().replace("_", "-") in _AUTH_KEYS and child:
                        raise ChatContinuationError(
                            "This call supplies authentication material as arguments. "
                            "Configure the tool's connection credential instead."
                        )
                    pending.append((child, depth + 1))
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
            elif isinstance(value, str) and _AUTH_TEXT.search(value):
                raise ChatContinuationError(
                    "This call contains authentication material. Use the configured tool connection instead."
                )
    except (ValueError, RecursionError) as exc:
        raise ChatContinuationError("This call's exact arguments are invalid or exceed the storage limit.") from exc
    return parsed
