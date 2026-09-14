"""Optional durable boundaries for the existing, non-streaming agent loop."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..model_evidence import MAX_RECORDED_MODEL_CALLS, ModelCallEvidence, ModelCallRecorder
from ..safety import MessageSafety
from ..usage.models import TokenUsage
from .approvals import ApprovalDraft
from .tools import ToolSpec

TURN_BUDGET_KEYS = frozenset({"document", "recall", "mcp", "web_calls", "web_chars"})


class CheckpointResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    content: str
    toolCalls: list[dict[str, Any]]
    outputItems: list[dict[str, Any]]
    incomplete: bool
    incompleteReason: str | None
    tail: bool = False


class TurnCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    version: Literal[1] = 1
    phase: Literal["ready", "model", "response", "tools", "finished"]
    conversation: list[dict[str, Any]]
    toolSchema: list[dict[str, Any]]
    offeredTools: list[dict[str, Any]]
    contracts: dict[str, str]
    steps: list[dict[str, Any]]
    iterations: int = Field(ge=0, le=3)
    completedModelCalls: int = Field(ge=0, le=3)
    toolCallsUsed: int = Field(ge=0, le=16)
    nextToolIndex: int = Field(ge=0, le=8)
    currentToolCounted: bool
    response: CheckpointResponse | None
    responseAppended: bool
    untrustedContext: bool
    forceFinal: bool
    deniedOnce: list[str]
    usage: TokenUsage
    effectivePrompt: list[dict[str, Any]]
    modelRequests: list[list[dict[str, Any]]] = Field(max_length=3)
    modelEvidence: list[ModelCallEvidence] = Field(max_length=MAX_RECORDED_MODEL_CALLS)
    modelEvidenceCount: int = Field(ge=0)
    safety: MessageSafety | None
    droppedContextMessages: int = Field(ge=0)
    completedText: list[str] = Field(max_length=3)
    capabilityBudgets: dict[str, int]

    @field_validator("capabilityBudgets", mode="before")
    @classmethod
    def complete_budget_state(cls, value: Any) -> Any:
        if (
            not isinstance(value, dict) or set(value) != TURN_BUDGET_KEYS
            or any(type(item) is not int or not 0 <= item <= 1_000_000 for item in value.values())
        ):
            raise ValueError("A durable turn requires complete, nonnegative capability budgets.")
        return value

    @classmethod
    def from_persisted(cls, raw: Any) -> TurnCheckpoint:
        if not isinstance(raw, Mapping) or not set(cls.model_fields).issubset(raw):
            raise ValueError("Incomplete persisted agent checkpoint.")
        return cls.model_validate(raw)


class CheckpointRecorder(ModelCallRecorder):
    """Restore immutable prior evidence without consulting today's price book."""

    def __init__(self, original: ModelCallRecorder, restored: TurnCheckpoint | None) -> None:
        super().__init__(
            model_id=original.model_id, deployment=original.deployment,
            pricing=original.pricing, model_source=original.model_source,
            parameter_source=original.parameter_source, overrides=original.overrides,
        )
        self._restored = list(restored.modelEvidence) if restored else []
        self.count = restored.modelEvidenceCount if restored else 0

    def snapshot(self) -> list[ModelCallEvidence]:
        return [*self._restored, *super().snapshot()][:MAX_RECORDED_MODEL_CALLS]


class TurnCheckpointController(Protocol):
    @property
    def restored(self) -> TurnCheckpoint | None: ...

    @property
    def visible_resource_ids(self) -> frozenset[str]: ...

    async def before_model(self, state: TurnCheckpoint, params: dict[str, Any]) -> None: ...

    async def model_completed(self, state: TurnCheckpoint) -> None: ...

    async def before_tool(
        self, state: TurnCheckpoint, *, tool: str, arguments: dict[str, Any],
        contract: str,
    ) -> None: ...

    async def tool_completed(self, state: TurnCheckpoint, *, outcome: str) -> None: ...

    async def hold(
        self, state: TurnCheckpoint, *, spec: ToolSpec, draft: ApprovalDraft,
        arguments: dict[str, Any], contract: str,
    ) -> None: ...
