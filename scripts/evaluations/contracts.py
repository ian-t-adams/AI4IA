"""Bounded versioned inputs and content-free outputs for offline evaluations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ROOT = Path(__file__).resolve().parents[2]
MAX_DATASET_BYTES = 131_072
MAX_REPORT_BYTES = 262_144
MAX_CASES = 32
MAX_CASE_SECONDS = 30
RUNNER_VERSION = "1.0.0"
ORACLE_VERSION = "1.0.0"
CHECK_IDS = (
    "execution", "transport", "tool_choice", "tool_feedback", "citations", "approval",
    "output_schema", "content", "safety", "cost", "latency", "workflow",
)
Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"),
]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Version = Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
Protocol = Literal["chat", "responses", "anthropic"]
Status = Literal["passed", "failed", "unknown", "unscored"]
Reason = Literal[
    "matched", "violated", "not_applicable", "evidence_missing", "timeout",
    "execution_error", "invalid_outcome", "missing_outcome", "not_run",
]
Count = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]


class EvaluationError(RuntimeError):
    """Only fixed, content-free error codes may cross the CLI boundary."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_assignment=True)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def bounded_json(path: Path, limit: int, code: str) -> object:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    return decode_json(data, limit, code)


def decode_json(data: bytes, limit: int, code: str) -> object:
    if len(data) > limit:
        raise EvaluationError(code)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EvaluationError("duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise EvaluationError("nonfinite_json")

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise EvaluationError("invalid_json") from None
    bounded_tree(value)
    return value


def bounded_tree(value: object) -> None:
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 20_000 or depth > 24:
            raise EvaluationError("json_structure_too_large")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str) and len(item.encode("utf-8")) > 16_384:
            raise EvaluationError("json_string_too_large")


class Reply(StrictModel):
    body: dict
    latency_ms: int = Field(ge=0, le=60_000, strict=True)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        bounded_tree(self.body)
        if len(canonical_bytes(self.body)) > 16_384:
            raise ValueError("fixture_too_large")
        return self


class DocumentFixture(StrictModel):
    filename: str = Field(pattern=r"^[a-z0-9_-]+\.md$", max_length=64)
    text: str = Field(min_length=1, max_length=4096)
    foreign_text: str = Field(min_length=1, max_length=4096)


class Expectations(StrictModel):
    status: Literal["complete", "error", "cancelled"] = "complete"
    required_tools: tuple[Identifier, ...] = Field(default=(), max_length=8)
    forbidden_tools: tuple[Identifier, ...] = Field(default=(), max_length=8)
    tool_results: dict[str, dict] = Field(default_factory=dict, max_length=8)
    require_tool_feedback: bool = False
    require_citations: bool = False
    output_schema: dict | None = None
    required_text: tuple[str, ...] = Field(default=(), max_length=8)
    forbidden_text: tuple[str, ...] = Field(default=(), max_length=8)
    safety_status: Literal["reported", "partial", "unavailable"] = "unavailable"
    safety_filtered: bool = False
    max_model_calls: int = Field(ge=1, le=16, strict=True)
    max_latency_ms: int = Field(ge=1, le=60_000, strict=True)
    max_cost_micro_usd: int = Field(ge=0, le=1_000_000, strict=True)
    workflow_steps: int | None = Field(default=None, ge=1, le=4, strict=True)

    @model_validator(mode="after")
    def safe_schema(self) -> Self:
        if set(self.required_tools) & set(self.forbidden_tools):
            raise ValueError("contradictory_tool_expectations")
        if any(len(text) > 1024 for text in (*self.required_text, *self.forbidden_text)):
            raise ValueError("oracle_text_too_large")
        if self.output_schema is not None:
            from jsonschema import Draft202012Validator
            from jsonschema.exceptions import SchemaError

            bounded_tree(self.output_schema)
            pending = [self.output_schema]
            while pending:
                item = pending.pop()
                if isinstance(item, dict):
                    if any(key in item for key in ("$ref", "$dynamicRef", "$recursiveRef")):
                        raise ValueError("schema_references_forbidden")
                    pending.extend(item.values())
                elif isinstance(item, list):
                    pending.extend(item)
            try:
                Draft202012Validator.check_schema(self.output_schema)
            except SchemaError:
                raise ValueError("invalid_output_schema") from None
        return self


class Case(StrictModel):
    id: Identifier
    scenario: Literal["chat", "agent", "workflow", "document", "approval", "safety"]
    protocol: Protocol
    stream: bool = False
    input: str = Field(min_length=1, max_length=4096)
    system_prompt: str = Field(default="Answer the synthetic task precisely.", max_length=4096)
    replies: tuple[Reply, ...] = Field(min_length=1, max_length=12)
    expected: Expectations
    document: DocumentFixture | None = None
    workflow_instructions: tuple[str, ...] = Field(default=(), max_length=4)
    approval_variant: Literal[
        "none", "exact-replay", "changed-arguments", "cross-session", "cross-owner",
    ] = "none"

    @model_validator(mode="after")
    def scenario_contract(self) -> Self:
        if (self.scenario == "document") != (self.document is not None):
            raise ValueError("document_fixture_required")
        if (self.scenario == "approval") != (self.approval_variant != "none"):
            raise ValueError("approval_variant_required")
        if self.scenario == "workflow":
            if len(self.workflow_instructions) != self.expected.workflow_steps:
                raise ValueError("workflow_steps_required")
        elif self.workflow_instructions or self.expected.workflow_steps is not None:
            raise ValueError("unexpected_workflow")
        if self.stream and self.protocol != "chat":
            raise ValueError("unsupported_stream_fixture")
        if self.scenario == "approval" and (self.protocol != "chat" or self.stream):
            raise ValueError("unsupported_approval_fixture")
        if self.scenario == "workflow" and self.stream:
            raise ValueError("unsupported_workflow_stream")
        if len(canonical_bytes(self.model_dump(mode="json"))) > 32_768:
            raise ValueError("case_too_large")
        return self


def applicable_checks(case: Case) -> set[str]:
    active = {"execution", "transport", "tool_choice", "safety", "cost", "latency"}
    for name, applies in (
        ("tool_feedback", case.expected.require_tool_feedback),
        ("citations", case.expected.require_citations),
        ("approval", case.scenario == "approval" or (
            case.scenario == "workflow" and case.expected.status == "error"
        )),
        ("output_schema", case.expected.output_schema is not None),
        ("content", bool(case.expected.required_text or case.expected.forbidden_text)),
        ("workflow", case.scenario == "workflow"),
    ):
        if applies:
            active.add(name)
    return active


class Configuration(StrictModel):
    id: Identifier = "offline-defaults"
    version: Version
    mode: Literal["offline"] = "offline"
    judge: Literal["disabled"] = "disabled"
    production_content: Literal[False] = False
    pricing_enabled: bool = True
    price_version: Identifier
    input_per_1m: float = Field(ge=0, le=100)
    output_per_1m: float = Field(ge=0, le=100)


class Dataset(StrictModel):
    schema_version: Literal[1]
    id: Identifier
    version: Version
    prompt_version: Version
    provenance: Literal["authored-synthetic-no-production-traces"]
    provider_versions: dict[Protocol, Identifier]
    config: Configuration
    cases: tuple[Case, ...] = Field(min_length=1, max_length=MAX_CASES)

    @model_validator(mode="after")
    def complete(self) -> Self:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate_case")
        if set(self.provider_versions) != {case.protocol for case in self.cases}:
            raise ValueError("provider_versions_required")
        if len(canonical_bytes(self.model_dump(mode="json"))) > MAX_DATASET_BYTES:
            raise ValueError("dataset_too_large")
        return self


def load_dataset() -> Dataset:
    return Dataset.model_validate(bounded_json(
        Path(__file__).with_name("datasets") / "synthetic-v1.json",
        MAX_DATASET_BYTES, "dataset_too_large",
    ))


class ModelIdentity(StrictModel):
    protocol: Protocol
    model_id: Identifier
    catalog_model_version: Identifier
    provider_fixture_version: Identifier
    provider_version_basis: Literal["synthetic-wire-contract-not-live-observation"] = (
        "synthetic-wire-contract-not-live-observation"
    )


class Identity(StrictModel):
    schema_version: Literal[1] = 1
    runner_version: Version
    oracle_version: Version
    mode: Literal["offline-synthetic"] = "offline-synthetic"
    live_quality: Literal["not_measured"] = "not_measured"
    judge: Literal["disabled"] = "disabled"
    content_export: Literal[False] = False
    dataset_id: Identifier
    dataset_version: Version
    dataset_sha256: Digest
    config_id: Identifier
    config_version: Version
    config_sha256: Digest
    prompt_version: Version
    prompt_sha256: Digest
    price_fixture_version: Identifier
    gateway_fixture_version: Identifier
    catalog_sha256: Digest
    evaluator_sha256: Digest
    application_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    application_tree_sha256: Digest
    environment_sha256: Digest
    case_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=MAX_CASES)
    models: tuple[ModelIdentity, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("duplicate_manifest_case")
        if len({model.protocol for model in self.models}) != len(self.models):
            raise ValueError("duplicate_model_protocol")
        return self


class Check(StrictModel):
    id: Identifier
    status: Status
    reason: Reason


class Measurements(StrictModel):
    model_calls: Count | None = None
    tool_calls: Count | None = None
    fixture_latency_ms: Count | None = None
    latency_basis: Literal["scripted-provider-ms-not-wall-clock"] = (
        "scripted-provider-ms-not-wall-clock"
    )
    cost_micro_usd: Count | None = None
    known_cost_subtotal_micro_usd: Count | None = None
    cost_coverage: Literal["known", "partial", "unknown"] = "unknown"
    cost_basis: Literal["synthetic-token-prices-not-billing"] = "synthetic-token-prices-not-billing"
    prompt_tokens: Count | None = None
    completion_tokens: Count | None = None

    @model_validator(mode="after")
    def honest_cost(self) -> Self:
        if (self.cost_coverage == "known") != (self.cost_micro_usd is not None):
            raise ValueError("cost_coverage_mismatch")
        if self.cost_coverage == "unknown" and self.known_cost_subtotal_micro_usd is not None:
            raise ValueError("unknown_cost_is_not_zero")
        return self


def overall(statuses: list[Status]) -> Status:
    for status in ("failed", "unknown", "passed"):
        if status in statuses:
            return status
    return "unscored"


class CaseResult(StrictModel):
    case_id: Identifier
    protocol: Protocol
    status: Status
    checks: tuple[Check, ...] = Field(min_length=len(CHECK_IDS), max_length=len(CHECK_IDS))
    measurements: Measurements = Field(default_factory=Measurements)
    instruction_hashes: tuple[Digest, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def complete(self) -> Self:
        if tuple(check.id for check in self.checks) != CHECK_IDS:
            raise ValueError("missing_or_duplicate_check")
        if self.status != overall([check.status for check in self.checks]):
            raise ValueError("case_status_mismatch")
        return self


def unknown_result(case: Case, reason: Reason) -> CaseResult:
    return CaseResult(
        case_id=case.id, protocol=case.protocol, status="unknown",
        checks=tuple(Check(id=name, status="unknown", reason=reason) for name in CHECK_IDS),
    )


class Coverage(StrictModel):
    total: Count
    passed: Count
    failed: Count
    unknown: Count
    unscored: Count
    scored: Count
    pass_numerator: Count
    pass_denominator: Count

    @model_validator(mode="after")
    def denominator(self) -> Self:
        if (
            self.passed + self.failed + self.unknown + self.unscored != self.total
            or self.scored != self.passed + self.failed
            or self.pass_numerator != self.passed
            or self.pass_denominator != self.total
        ):
            raise ValueError("coverage_mismatch")
        return self


def coverage(statuses: list[Status]) -> Coverage:
    passed, failed = statuses.count("passed"), statuses.count("failed")
    return Coverage(
        total=len(statuses), passed=passed, failed=failed,
        unknown=statuses.count("unknown"), unscored=statuses.count("unscored"),
        scored=passed + failed, pass_numerator=passed, pass_denominator=len(statuses),
    )


class Report(StrictModel):
    identity: Identity
    cases: tuple[CaseResult, ...] = Field(min_length=1, max_length=MAX_CASES)
    coverage: Coverage
    check_coverage: dict[str, Coverage]
    gate: Literal["passed", "failed", "unknown"]

    @model_validator(mode="after")
    def honest(self) -> Self:
        if tuple(case.case_id for case in self.cases) != self.identity.case_ids:
            raise ValueError("case_manifest_mismatch")
        if {model.protocol for model in self.identity.models} != {
            case.protocol for case in self.cases
        }:
            raise ValueError("missing_model_identity")
        if self.coverage != coverage([case.status for case in self.cases]):
            raise ValueError("summary_mismatch")
        if self.check_coverage != {
            name: coverage([case.checks[index].status for case in self.cases])
            for index, name in enumerate(CHECK_IDS)
        }:
            raise ValueError("check_summary_mismatch")
        expected_gate = (
            "failed" if self.coverage.failed else
            "unknown" if self.coverage.unknown or self.coverage.unscored else "passed"
        )
        if self.gate != expected_gate:
            raise ValueError("gate_mismatch")
        return self


def make_report(identity: Identity, rows: list[CaseResult]) -> Report:
    summary = coverage([case.status for case in rows])
    return Report(
        identity=identity, cases=tuple(rows), coverage=summary,
        check_coverage={
            name: coverage([case.checks[index].status for case in rows])
            for index, name in enumerate(CHECK_IDS)
        },
        gate="failed" if summary.failed else "unknown" if summary.unknown or summary.unscored else "passed",
    )


class Comparison(StrictModel):
    schema_version: Literal[1] = 1
    case_count: Count
    regressed: list[Identifier]
    improved: list[Identifier]
    unknown: list[Identifier]
