"""Separate live authored-synthetic contracts; never import the API or offline worker."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import math
import os
import re
import time
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .contracts import (
    CHECK_IDS, ROOT, Check, Count, Coverage, Digest, EvaluationError, Identifier, Status,
    StrictModel, Version, bounded_json, canonical_bytes, coverage, decode_json, digest, overall,
)

LIVE_VERSION = "1.0.0"
LIVE_CHECK_IDS = (*CHECK_IDS, "isolation", "cleanup")
MAX_HTTP_REQUESTS = 48
MAX_RUN_SECONDS = 240
MAX_CASE_SECONDS = 45
CLEANUP_REQUEST_RESERVE = 8
CLEANUP_SECONDS_RESERVE = 45
MAX_RESPONSE_BYTES = 98_304
MAX_TOTAL_RESPONSE_BYTES = 1_048_576
MAX_REQUEST_BYTES = 8_192
MAX_OUTPUT_TOKENS = 256
MAX_ESTIMATE_MICRO_USD = 100_000
MAX_RECONCILES = 3
MAX_LIVE_REPORT_BYTES = 65_536
UUIDText = Annotated[str, Field(pattern=r"^[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$")]
FailureCode = Literal[
    "configuration", "identity", "catalog", "pricing", "capability", "transport",
    "http", "timeout", "bounds", "shape", "cleanup", "worker", "disabled", "not_run",
]


class LiveError(EvaluationError):
    def __init__(self, code: FailureCode) -> None:
        super().__init__(code)
        self.code: FailureCode = code


class LiveCase(StrictModel):
    id: Identifier
    profile: Literal["no-tools-no-memory"]
    instructions: str = Field(min_length=1, max_length=1024)
    input: str = Field(min_length=1, max_length=1024)
    output_schema: dict | None = None
    exact_text: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def oracle(self) -> Self:
        from .contracts import Expectations

        if (self.output_schema is None) == (self.exact_text is None):
            raise ValueError("exactly_one_oracle_required")
        # Reuse the offline schema guard, including no external references.
        Expectations(
            output_schema=self.output_schema, max_model_calls=1,
            max_latency_ms=45_000, max_cost_micro_usd=MAX_ESTIMATE_MICRO_USD,
        )
        if len(authored_prompt(self).encode("utf-8")) > 4096:
            raise ValueError("authored_prompt_too_large")
        return self


def authored_prompt(case: LiveCase) -> str:
    return (
        f"Task specification:\n{case.instructions}\n\n"
        f"Quoted task data:\n{json.dumps(case.input, ensure_ascii=True)}"
    )


class LiveDataset(StrictModel):
    schema_version: Literal[1]
    id: Identifier
    version: Version
    prompt_version: Version
    provenance: Literal["authored-synthetic-no-production-traces"]
    cases: tuple[LiveCase, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate_case")
        return self


def load_live_dataset() -> LiveDataset:
    return LiveDataset.model_validate(bounded_json(
        ROOT / "scripts" / "evaluations" / "datasets" / "live-synthetic-v1.json",
        16_384, "dataset_too_large",
    ))


def applicable_live_checks(case: LiveCase) -> set[str]:
    return {
        "execution", "transport", "isolation", "cost", "latency", "cleanup",
        "output_schema" if case.output_schema is not None else "content",
    }


class LiveConfig(StrictModel):
    enabled: Literal["true"]
    api_origin: str = Field(min_length=1, max_length=253)
    api_audience: str = Field(min_length=1, max_length=256)
    tenant_id: UUIDText
    client_id: UUIDText
    actor_object_id: UUIDText
    deployment_client_id: UUIDText
    model_id: Identifier
    limits_ack: Literal["finite-requests-not-a-bill-cap"]

    @model_validator(mode="after")
    def dedicated_origin(self) -> Self:
        origin = urlsplit(self.api_origin)
        if (
            origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path not in ("", "/") or origin.query or origin.fragment
            or origin.port not in (None, 443)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", origin.hostname)
            or "." not in origin.hostname or ".." in origin.hostname
        ):
            raise ValueError("invalid_api_origin")
        try:
            ipaddress.ip_address(origin.hostname)
        except ValueError:
            pass
        else:
            raise ValueError("api_origin_requires_dns_name")
        if self.client_id.lower() == self.deployment_client_id.lower():
            raise ValueError("deployment_identity_forbidden")
        audience = self.api_audience.removeprefix("api://")
        if not re.fullmatch(r"[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", audience):
            raise ValueError("invalid_api_audience")
        return self

    def public_digest(self) -> str:
        # Actor/origin/token hashes would themselves be correlation identifiers.
        # Target/actor identity is deliberately absent; live reports cannot be
        # used for automatic cross-environment or model-version comparisons.
        return digest({
            "version": LIVE_VERSION, "model": self.model_id,
            "profile": "no-tools-no-memory", "limits": limits(),
        })


ENV_FIELDS = {
    "enabled": "AI4IA_LIVE_EVAL_ENABLED",
    "api_origin": "AI4IA_LIVE_EVAL_API_ORIGIN",
    "api_audience": "AI4IA_LIVE_EVAL_API_AUDIENCE",
    "tenant_id": "AI4IA_LIVE_EVAL_TENANT_ID",
    "client_id": "AI4IA_LIVE_EVAL_CLIENT_ID",
    "actor_object_id": "AI4IA_LIVE_EVAL_ACTOR_OBJECT_ID",
    "deployment_client_id": "AI4IA_LIVE_EVAL_DEPLOY_CLIENT_ID",
    "model_id": "AI4IA_LIVE_EVAL_MODEL_ID",
    "limits_ack": "AI4IA_LIVE_EVAL_LIMITS_ACK",
}
TOKEN_ENV = "AI4IA_LIVE_EVAL_TOKEN"


def config_from_environment() -> LiveConfig:
    if os.environ.get(ENV_FIELDS["enabled"]) != "true":
        raise LiveError("disabled")
    return LiveConfig.model_validate({key: os.environ.get(env) for key, env in ENV_FIELDS.items()})


def bind_token(config: LiveConfig, token: str, *, now: float | None = None) -> None:
    """Local misconfiguration guard, NOT JWT signature verification.

    The governed API independently validates the token signature and authority.
    Do not describe decoded claims here as successful authentication.
    """
    if not isinstance(token, str) or len(token) > 8192 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
        raise LiveError("identity")
    encoded = token.split(".")[1]
    try:
        body = decode_json(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)),
            8192, "token_too_large",
        )
    except (EvaluationError, ValueError, binascii.Error):
        raise LiveError("identity") from None
    if not isinstance(body, dict):
        raise LiveError("identity")
    if not all(isinstance(body.get(key), str) for key in ("tid", "oid", "aud", "iss")):
        raise LiveError("identity")
    audience = config.api_audience.removeprefix("api://")
    clock = time.time() if now is None else now
    expires, not_before = body.get("exp"), body.get("nbf", 0)
    clients = [body[key] for key in ("appid", "azp") if key in body]
    if (
        body.get("tid", "").lower() != config.tenant_id.lower()
        or body.get("oid", "").lower() != config.actor_object_id.lower()
        or not clients or any(
            not isinstance(value, str) or value.lower() != config.client_id.lower()
            for value in clients
        )
        or body.get("aud") not in (audience, f"api://{audience}")
        or body.get("iss") not in (
            f"https://sts.windows.net/{config.tenant_id}/",
            f"https://login.microsoftonline.com/{config.tenant_id}/v2.0",
        )
        or "scp" in body
        or type(expires) is not int or expires <= clock + MAX_RUN_SECONDS + 30
        or type(not_before) is not int or not_before > clock
    ):
        raise LiveError("identity")


def limits() -> dict[str, int]:
    return {
        "http_requests": MAX_HTTP_REQUESTS, "run_seconds": MAX_RUN_SECONDS,
        "case_seconds": MAX_CASE_SECONDS, "request_bytes": MAX_REQUEST_BYTES,
        "response_bytes": MAX_RESPONSE_BYTES, "total_response_bytes": MAX_TOTAL_RESPONSE_BYTES,
        "output_tokens_per_request": MAX_OUTPUT_TOKENS,
        "estimate_micro_usd_per_case": MAX_ESTIMATE_MICRO_USD,
        "cleanup_request_reserve": CLEANUP_REQUEST_RESERVE,
        "cleanup_seconds_reserve": CLEANUP_SECONDS_RESERVE, "reconciles_per_session": MAX_RECONCILES,
    }


def source_documents() -> tuple[dict, dict]:
    catalog = bounded_json(ROOT / "infra" / "models.json", 262_144, "catalog_too_large")
    prices = bounded_json(
        ROOT / "app" / "api" / "src" / "ai4ia_api" / "data" / "pricing.json",
        131_072, "pricing_too_large",
    )
    if not isinstance(catalog, dict) or not isinstance(prices, dict):
        raise LiveError("catalog")
    return catalog, prices


def source_model(config: LiveConfig, catalog: dict, prices: dict) -> dict:
    entries = catalog.get("catalog")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise LiveError("catalog")
    candidates = [entry for entry in entries if entry.get("name") == config.model_id]
    if (
        len(candidates) != 1 or candidates[0].get("api", "chat") != "chat"
        or candidates[0].get("category") not in ("chat", "chat-fast")
        or not candidates[0].get("deployments")
    ):
        raise LiveError("catalog")
    price = prices.get("models", {}).get(config.model_id)
    if (
        prices.get("currency") != "USD" or not prices.get("version")
        or not isinstance(price, dict)
        or not all(
            type(price.get(key)) in (int, float) and math.isfinite(price[key]) and 0 < price[key] <= 1000
            for key in ("inputPer1M", "outputPer1M")
        )
    ):
        raise LiveError("pricing")
    return candidates[0]


class LiveIdentity(StrictModel):
    schema_version: Literal[1] = 1
    mode: Literal["live-authored-synthetic"] = "live-authored-synthetic"
    runner_version: Version = LIVE_VERSION
    oracle_version: Version = LIVE_VERSION
    dataset_id: Identifier
    dataset_version: Version
    prompt_version: Version
    dataset_sha256: Digest
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_sha256: Digest
    catalog_sha256: Digest
    pricing_sha256: Digest
    environment_sha256: Digest
    config_sha256: Digest | None = None
    model_id: Identifier | None = None
    catalog_model_versions: tuple[Identifier, ...] = Field(default=(), max_length=24)
    advertised_catalog_sha256: Digest | None = None
    execution_capabilities_version: Literal[1] | None = None
    request_reductions_version: Literal[1] | None = None
    model_version_basis: Literal["source-and-advertised-configuration-not-provider-observation"] = (
        "source-and-advertised-configuration-not-provider-observation"
    )
    provider_observed_model_version: None = None
    deployed_application_revision: None = None
    comparison: Literal["unsupported-unidentified-live-provider-and-target"] = (
        "unsupported-unidentified-live-provider-and-target"
    )
    quality_scope: Literal["three-authored-no-tool-oracles-only"] = "three-authored-no-tool-oracles-only"
    open_ended_quality: Literal["unmeasured"] = "unmeasured"
    judge: Literal["disabled"] = "disabled"
    production_content: Literal[False] = False
    content_export: Literal[False] = False
    case_ids: tuple[Identifier, ...] = Field(min_length=3, max_length=3)


class LiveMeasurements(StrictModel):
    http_attempts: Count | None = None
    latency_ms: Count | None = None
    latency_basis: Literal["governed-api-chat-wall-clock"] = "governed-api-chat-wall-clock"
    cost_micro_usd: Count | None = None
    cost_basis: Literal["receipt-snapshot-token-estimate-not-billing"] = "receipt-snapshot-token-estimate-not-billing"
    prompt_tokens: Count | None = None
    completion_tokens: Count | None = None
    model_calls: Count | None = None
    instruction_sha256: Digest | None = None


class LiveResult(StrictModel):
    case_id: Identifier
    status: Status
    checks: tuple[Check, ...] = Field(min_length=len(LIVE_CHECK_IDS), max_length=len(LIVE_CHECK_IDS))
    measurements: LiveMeasurements = Field(default_factory=LiveMeasurements)

    @model_validator(mode="after")
    def complete(self) -> Self:
        if tuple(check.id for check in self.checks) != LIVE_CHECK_IDS:
            raise ValueError("missing_check")
        if self.status != overall([check.status for check in self.checks]):
            raise ValueError("case_status_mismatch")
        return self


def unknown_live(case: LiveCase) -> LiveResult:
    return LiveResult(
        case_id=case.id, status="unknown",
        checks=tuple(Check(id=name, status="unknown", reason="not_run") for name in LIVE_CHECK_IDS),
    )


class LifecycleObservation(StrictModel):
    status: Literal["passed", "unknown", "not_run"] = "not_run"
    http_attempts: Count | None = None
    scope: Literal["new-empty-session-only-not-physical-erasure"] = (
        "new-empty-session-only-not-physical-erasure"
    )


class CapabilityConstraints(StrictModel):
    allowTools: bool = Field(strict=True)
    allowAutomaticMemory: bool = Field(strict=True)
    requireFreshSession: bool = Field(strict=True)
    maxOutputTokens: int = Field(strict=True, ge=MAX_OUTPUT_TOKENS, le=MAX_OUTPUT_TOKENS)
    libraryDocumentIds: tuple[Identifier, ...] = Field(max_length=0)

    @model_validator(mode="after")
    def reductions(self) -> Self:
        if self.allowTools or self.allowAutomaticMemory or not self.requireFreshSession:
            raise ValueError("unsafe_execution_constraints")
        return self


class ExecutionCapabilities(StrictModel):
    version: int = Field(strict=True, ge=1, le=1)
    ready: bool = Field(strict=True)
    ownerBound: bool = Field(strict=True)
    profile: Literal["authored-synthetic-evaluation"]
    model: Identifier
    api: Literal["chat"]
    region: Identifier
    reductionControlsVersion: int = Field(strict=True, ge=1, le=1)
    constraints: CapabilityConstraints
    reason: None = None

    @model_validator(mode="after")
    def admitted(self) -> Self:
        if not self.ready or not self.ownerBound:
            raise ValueError("execution_policy_unavailable")
        return self


class LiveReport(StrictModel):
    identity: LiveIdentity
    cases: tuple[LiveResult, ...] = Field(min_length=3, max_length=3)
    coverage: Coverage
    check_coverage: dict[str, Coverage]
    complete: bool
    gate: Literal["passed", "failed", "unknown"]
    termination: Literal["complete"] | FailureCode
    lifecycle: LifecycleObservation
    operation: Literal["run", "preflight"] = "run"
    api_preflight: Literal["passed", "unknown", "not_run"] = "not_run"
    http_attempts: Count | None
    response_bytes: Count | None
    limits: dict[str, int]
    bill_cap: Literal["not_proven"] = "not_proven"
    schedule_policy: Literal["separate-opt-in-non-pr-blocking"] = "separate-opt-in-non-pr-blocking"

    @model_validator(mode="after")
    def honest(self) -> Self:
        dataset = load_live_dataset()
        if (
            self.identity.case_ids != tuple(case.id for case in dataset.cases)
            or self.identity.dataset_sha256 != digest(dataset.model_dump(mode="json"))
        ):
            raise ValueError("dataset_manifest_mismatch")
        if tuple(case.case_id for case in self.cases) != self.identity.case_ids:
            raise ValueError("case_manifest_mismatch")
        for source, row in zip(dataset.cases, self.cases):
            active = applicable_live_checks(source)
            if any(
                check.id in active and check.status == "unscored"
                or check.id not in active and check.status not in ("unscored", "unknown")
                for check in row.checks
            ):
                raise ValueError("incompatible_scoring_coverage")
        if self.coverage != coverage([case.status for case in self.cases]):
            raise ValueError("coverage_mismatch")
        if self.check_coverage != {
            name: coverage([case.checks[index].status for case in self.cases])
            for index, name in enumerate(LIVE_CHECK_IDS)
        }:
            raise ValueError("check_coverage_mismatch")
        if self.operation == "preflight" and (
            self.lifecycle.status != "not_run"
            or any(row.status != "unknown" for row in self.cases)
        ):
            raise ValueError("preflight_cannot_measure_quality_or_create_fixtures")
        complete = (
            self.operation == "run" and self.api_preflight == "passed"
            and self.termination == "complete" and self.lifecycle.status == "passed" and all(
            all(check.status != "unknown" for check in row.checks)
            and row.checks[-1].status == "passed" for row in self.cases
            )
        )
        expected = "unknown" if not complete else "failed" if self.coverage.failed else "passed"
        if self.complete != complete or self.gate != expected:
            raise ValueError("gate_mismatch")
        if (
            self.limits != limits()
            or self.http_attempts is not None and self.http_attempts > MAX_HTTP_REQUESTS
            or self.response_bytes is not None and self.response_bytes > MAX_TOTAL_RESPONSE_BYTES
            or complete and (self.http_attempts is None or self.response_bytes is None)
        ):
            raise ValueError("limits_mismatch")
        if len(canonical_bytes(self.model_dump(mode="json"))) > MAX_LIVE_REPORT_BYTES:
            raise ValueError("report_too_large")
        return self
