"""Bounded native SystemOne contracts. No chat adaptation or generated rationale."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .usage.models import TokenUsage

MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 65_536
MAX_INPUT_CHARS = 16_000
MAX_QUESTIONS = 8
MAX_OPTIONS = 12
DECISION_PROVIDER = "microsoft_decision"

Name = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")]
Description = Annotated[str, Field(min_length=1, max_length=512)]


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object keys must be unique.")
        result[key] = value
    return result


def reject_constant(_: str) -> None:
    raise ValueError("JSON numbers must be finite.")


def read_json(value: str | bytes) -> Any:
    return json.loads(value, object_pairs_hook=unique_object, parse_constant=reject_constant)


class DecisionQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: Name
    type: Literal["noul", "choice", "score"]
    instructions: str = Field(min_length=1, max_length=2000)
    criteria: dict[Name, Description] | list[Description] | None = None

    @model_validator(mode="after")
    def check_criteria(self) -> DecisionQuestion:
        if not self.instructions.strip():
            raise ValueError("Question instructions must not be blank.")
        criteria = self.criteria
        if self.type == "noul":
            if criteria is not None and (
                not isinstance(criteria, dict) or set(criteria) != {"true", "false"}
            ):
                raise ValueError("Yes/no criteria must define both true and false, or be omitted.")
        elif self.type == "choice":
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= MAX_OPTIONS:
                raise ValueError("Choice needs 2 to 12 named options.")
        elif not isinstance(criteria, list) or not 2 <= len(criteria) <= 10:
            raise ValueError("Score needs 2 to 10 ordered levels.")
        values = list(criteria.values()) if isinstance(criteria, dict) else criteria or []
        if any(not value.strip() for value in values):
            raise ValueError("Criteria descriptions must not be blank.")
        if isinstance(criteria, list) and len(set(criteria)) != len(criteria):
            raise ValueError("Score levels must be distinct.")
        return self

    def native(self) -> dict[str, Any]:
        # State remains a separate data value; it is never interpolated into instructions.
        return self.model_dump(exclude={"name"}, exclude_none=True)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: str = Field(min_length=1, max_length=128)
    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    inputFormat: Literal["text", "json"] = "text"
    questions: list[DecisionQuestion] = Field(min_length=1, max_length=MAX_QUESTIONS)

    @model_validator(mode="after")
    def check_request(self) -> DecisionRequest:
        if not self.input.strip():
            raise ValueError("Input must not be blank.")
        if len({q.name for q in self.questions}) != len(self.questions):
            raise ValueError("Question names must be unique.")
        self.native(self.model)
        return self

    def native(self, deployment: str) -> dict[str, Any]:
        try:
            state = read_json(self.input) if self.inputFormat == "json" else self.input
            pending = [(state, 0)]
            nodes = 0
            while pending:
                item, depth = pending.pop()
                nodes += 1
                if depth > 16 or nodes > 4096:
                    raise ValueError("JSON input exceeds 16 levels or 4096 values.")
                if isinstance(item, dict):
                    pending.extend((child, depth + 1) for child in item.values())
                elif isinstance(item, list):
                    pending.extend((child, depth + 1) for child in item)
                elif isinstance(item, float) and not math.isfinite(item):
                    raise ValueError("JSON numbers must be finite.")
            if state is None:
                raise ValueError("JSON input must not be null.")
            payload = {
                "model": deployment, "state": state,
                "questions": {q.name: q.native() for q in self.questions},
            }
            if len(json.dumps(payload, ensure_ascii=True, allow_nan=False).encode()) > MAX_REQUEST_BYTES:
                raise ValueError("Decision request exceeds 64 KiB.")
            return payload
        except (RecursionError, UnicodeError) as exc:
            raise ValueError("Input is not bounded valid JSON text.") from exc


def decision_usage(raw: Any) -> TokenUsage:
    """Only native, complete integer counts become known ledger usage."""
    if not isinstance(raw, dict) or any(
        type(raw.get(key)) is not int or not 0 <= raw[key] <= 2**52 - 1
        for key in ("input_tokens", "output_tokens")
    ):
        return TokenUsage(known=False, complete=False, calls=1)
    return TokenUsage(
        prompt=raw["input_tokens"], completion=raw["output_tokens"],
        total=raw["input_tokens"] + raw["output_tokens"],
        known=True, complete=True, calls=1,
    )


def probability(value: Any) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
        raise ValueError("Invalid decision probability.")
    return float(value)


def validate_answers(raw: Any, questions: list[DecisionQuestion]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {q.name for q in questions}:
        raise ValueError("Decision answer names do not match.")
    result: dict[str, Any] = {}
    for question in questions:
        answer = raw[question.name]
        if not isinstance(answer, dict) or answer.get("type") != question.type:
            raise ValueError("Decision answer type does not match.")
        if question.type == "noul":
            result[question.name] = {"type": "noul", "noul": probability(answer.get("noul"))}
            continue
        criteria = question.criteria
        assert isinstance(criteria, (list, dict))
        keys = list(criteria) if isinstance(criteria, dict) else [str(i) for i in range(len(criteria))]
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != set(keys):
            raise ValueError("Decision distribution does not match the criteria.")
        distribution = {key: probability(probabilities[key]) for key in keys}
        values = list(distribution.values())
        # Native distributions may be rounded to hundredths; do not renormalize evidence.
        tolerance = len(values) * .005 + 1e-12 if all(v == round(v, 2) for v in values) else .001
        if not math.isclose(math.fsum(values), 1, rel_tol=0, abs_tol=tolerance):
            raise ValueError("Invalid decision distribution total.")
        clean: dict[str, Any] = {"type": question.type, "probabilities": distribution}
        if question.type == "choice":
            if answer.get("choice") not in keys:
                raise ValueError("Decision selected an unknown option.")
            clean["choice"] = answer["choice"]
        else:
            score = answer.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= len(keys) - 1 or not math.isfinite(score):
                raise ValueError("Invalid decision score.")
            clean["score"] = score
        if "confidence" in answer:
            clean["confidence"] = probability(answer["confidence"])
        result[question.name] = clean
    return result


@dataclass
class DecisionExecution:
    dispatched: bool = False
    provider_completed: bool = False
    usage: TokenUsage = field(default_factory=lambda: TokenUsage(known=False, complete=False, calls=1))
