"""Strict interchange contracts; application payloads never become reports."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

VERSION = 1
WORKFLOW = ".github/workflows/application-canaries.yml"
INTERVAL_SECONDS = 6 * 60 * 60
MAX_RUNS = 28
MAX_REPORT_BYTES = 32 * 1024
MAX_HTTP_BYTES = 256 * 1024
MAX_OUTPUT_TOKENS = 64
MAX_SECONDS = 120
THRESHOLD = 3
STAGES = (
    "platform", "auth", "catalog", "posture", "session",
    "gateway", "model", "persistence", "cleanup", "realtime",
)
OUTCOMES = frozenset({"pass", "fail", "unknown", "not_run", "partial"})
CODES = frozenset({
    "ok", "disabled", "not_ready", "prior_stage", "invalid_configuration",
    "identity_rejected", "auth_rejected", "network_unavailable", "deadline",
    "redirect_rejected", "invalid_response", "response_too_large",
    "no_compatible_model", "unpriced", "hard_bill_cap_unsupported",
    "posture_unavailable", "scope_changed", "state_missing", "state_invalid",
    "state_stale", "state_gap", "state_blocked", "cadence", "lease_exhausted",
    "approval_expired", "bootstrap", "control", "session_rejected",
    "create_unknown", "dispatch_unknown", "gateway_unavailable", "model_failed",
    "receipt_incomplete", "persistence_failed", "cleanup_pending",
    "logical_deleted", "cleanup_verified", "cleanup_failed",
    "ga_not_selected", "ga_unavailable", "protocol_mismatch", "protocol_error",
    "event_order", "event_limit", "closed", "cancelled", "collection_failed",
})
Outcome = Literal["pass", "fail", "unknown", "not_run", "partial"]
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class CanaryError(Exception):
    """Only an allowlisted code crosses the reporting boundary."""

    def __init__(self, code: str) -> None:
        if code not in CODES:
            raise ValueError("Unregistered canary error code.")
        super().__init__(code)
        self.code = code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        raise CanaryError("invalid_response")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise CanaryError("invalid_response") from exc


def integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise CanaryError("invalid_response")
    return value


def obj(value: Any, keys: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or (keys is not None and set(value) != keys):
        raise CanaryError("invalid_response")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanaryError("invalid_response")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise CanaryError("invalid_response")


def strict_json(raw: bytes, *, limit: int = MAX_REPORT_BYTES) -> Any:
    if len(raw) > limit:
        raise CanaryError("response_too_large")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CanaryError("invalid_response") from exc
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 16 or nodes > 20_000:
            raise CanaryError("invalid_response")
        if isinstance(item, float) and not math.isfinite(item):
            raise CanaryError("invalid_response")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def encoded(value: Any, *, limit: int = MAX_REPORT_BYTES) -> bytes:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    strict_json(raw, limit=limit)
    return raw


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value, limit=MAX_HTTP_BYTES)).hexdigest()


def public_origin(value: str) -> str:
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f\\]", value):
        raise CanaryError("invalid_configuration")
    try:
        parsed = urlsplit(value)
        valid_port = parsed.port in (None, 443)
    except ValueError as exc:
        raise CanaryError("invalid_configuration") from exc
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https" or not valid_port
        or parsed.netloc != host or parsed.path not in ("", "/")
        or parsed.query or parsed.fragment or parsed.username is not None
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host)
        or "." not in host or ".." in host or host.endswith((".localhost", ".local"))
    ):
        raise CanaryError("invalid_configuration")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return f"https://{host}"
    raise CanaryError("invalid_configuration")


@dataclass(frozen=True)
class Run:
    repository: str
    repository_id: int
    run_id: int
    number: int
    attempt: int
    sha: str

    def validate(self) -> None:
        if not isinstance(self.repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise CanaryError("invalid_configuration")
        for value in (self.repository_id, self.run_id, self.number):
            integer(value, 1, 2**63 - 1)
        if self.attempt != 1 or type(self.attempt) is not int:
            # A rerun may replay an accepted request even if the last job was red.
            raise CanaryError("state_invalid")
        if not isinstance(self.sha, str) or not re.fullmatch(r"[0-9a-f]{40}", self.sha):
            raise CanaryError("invalid_configuration")

    @classmethod
    def parse(cls, value: Any) -> Run:
        result = cls(**obj(value, set(cls.__dataclass_fields__)))
        result.validate()
        return result


@dataclass
class Stage:
    outcome: Outcome = "not_run"
    code: str = "prior_stage"
    observed_at: str | None = None
    latency_ms: int | None = None
    attempts: int = 0

    def validate(self) -> None:
        if (
            not isinstance(self.outcome, str) or self.outcome not in OUTCOMES
            or not isinstance(self.code, str) or self.code not in CODES
        ):
            raise CanaryError("invalid_response")
        integer(self.attempts, 0, 16)
        if self.observed_at is not None:
            timestamp(self.observed_at)
        if self.latency_ms is not None:
            integer(self.latency_ms, 0, MAX_SECONDS * 1000)
        if self.outcome == "pass" and (
            self.code not in {"ok", "cleanup_verified"}
            or self.observed_at is None
            or self.latency_ms is None
        ):
            raise CanaryError("invalid_response")


@dataclass
class Report:
    run: Run
    observed_at: str
    stages: dict[str, Stage] = field(default_factory=lambda: {key: Stage() for key in STAGES})
    version: int = VERSION
    catalog_version: str | None = None
    protocol: str | None = None
    chat_attempts: int = 0
    realtime_attempts: int = 0
    cleanup_safe: bool = True
    usage_known: bool = False
    estimated_micro_usd: int | None = None
    price_version: str | None = None
    coverage: str = "unscored"

    def mark(
        self, stage: str, outcome: Outcome, code: str = "ok", *,
        elapsed: float = 0, attempts: int = 0, at: datetime | None = None,
    ) -> None:
        if stage not in STAGES:
            raise ValueError("Unregistered canary stage.")
        row = Stage(
            outcome, code, stamp(at or utc_now()),
            min(MAX_SECONDS * 1000, max(0, round(elapsed * 1000))), attempts,
        )
        row.validate()
        self.stages[stage] = row

    def unobserved(self, code: str) -> None:
        for stage in STAGES:
            self.mark(stage, "not_run", code)

    def validate(self) -> None:
        self.run.validate()
        timestamp(self.observed_at)
        if type(self.version) is not int or self.version != VERSION or set(self.stages) != set(STAGES):
            raise CanaryError("invalid_response")
        for stage in self.stages.values():
            stage.validate()
        if self.catalog_version is not None and (
            not isinstance(self.catalog_version, str) or not SHA256.fullmatch(self.catalog_version)
        ):
            raise CanaryError("invalid_response")
        if self.protocol not in (None, "chat", "responses", "ga"):
            raise CanaryError("invalid_response")
        integer(self.chat_attempts, 0, 1)
        integer(self.realtime_attempts, 0, 1)
        for value in (self.cleanup_safe, self.usage_known):
            if type(value) is not bool:
                raise CanaryError("invalid_response")
        if self.estimated_micro_usd is not None:
            integer(self.estimated_micro_usd, 0, 2**53 - 1)
        if self.price_version is not None and (
            not isinstance(self.price_version, str) or not IDENTIFIER.fullmatch(self.price_version)
        ):
            raise CanaryError("invalid_response")
        if self.usage_known != (self.estimated_micro_usd is not None and self.price_version is not None):
            raise CanaryError("invalid_response")
        if self.coverage not in ("complete", "partial", "unscored"):
            raise CanaryError("invalid_response")
        if self.coverage == "complete" and any(s.outcome != "pass" for s in self.stages.values()):
            raise CanaryError("invalid_response")

    def document(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def parse(cls, value: Any) -> Report:
        data = obj(value, set(cls.__dataclass_fields__)).copy()
        data["run"] = Run.parse(data["run"])
        data["stages"] = {
            key: Stage(**obj(row, set(Stage.__dataclass_fields__)))
            for key, row in obj(data["stages"], set(STAGES)).items()
        }
        result = cls(**data)
        result.validate()
        return result

    def markdown(self) -> str:
        self.validate()
        rows = [
            "## Application canary",
            "",
            f"Observed: {self.observed_at}. Coverage: **{self.coverage}**.",
            "",
            "| Stage | Outcome | Code | Latency ms |",
            "|---|---|---|---|",
        ]
        rows.extend(
            f"| {name} | {row.outcome} | {row.code} | "
            f"{row.latency_ms if row.latency_ms is not None else 'unknown'} |"
            for name, row in self.stages.items()
        )
        rows.extend([
            "",
            "One application chat attempt at most; no provider-attempt or hard USD cap is claimed.",
            "Realtime covers setup events only, not audio, response generation, or persistence.",
            "Legacy logical deletion is not physical cleanup proof. Document ingestion is not covered.",
            "",
        ])
        return "\n".join(rows)
