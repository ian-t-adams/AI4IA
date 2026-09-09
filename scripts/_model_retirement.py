"""Pure, bounded retirement observations shared by preflight and read-only reports."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

POLICY_VERSION = "retirement-admission-v1"
BLOCK_WITHIN_DAYS = 7
MAX_OBSERVATIONS = 256
MAX_REPORT_BYTES = 512 * 1024
MAX_MARKDOWN_BYTES = 256 * 1024
MAX_DOC_PREVIEW_BYTES = MAX_MARKDOWN_BYTES + 64 * 1024
MAX_PUBLIC_BYTES = 128 * 1024
MAX_PUBLIC_RECORDS = 256
MODELS_REFERENCE = (
    "https://learn.microsoft.com/rest/api/aiservices/accountmanagement/models/list"
    "?view=rest-aiservices-accountmanagement-2024-10-01"
)
DOC_START = "<!-- model-retirements:start -->"
DOC_END = "<!-- model-retirements:end -->"
UNSAFE_LIFECYCLE = frozenset({"deprecating", "deprecated"})
KNOWN_LIFECYCLE = UNSAFE_LIFECYCLE | {"generallyavailable", "preview", "stable"}
Window = Literal["unknown", "expired", "7-day", "30-day", "90-day", "beyond-90-days"]
DateState = Literal["known", "missing", "malformed", "unsupported", "unavailable"]
InventoryState = Literal["observed", "unavailable", "not-requested"]
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}\Z")
_FORMAT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_INSTANT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z",
    re.IGNORECASE,
)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An observation timestamp must include a UTC offset.")
    return value.astimezone(UTC)


def timestamp(value: datetime) -> str:
    return utc(value).isoformat().replace("+00:00", "Z")


def parse_date(value: Any) -> tuple[datetime | None, DateState, str | None]:
    """Date-only means 00:00 UTC; a timestamp without a known offset is unknown."""
    if value is None or value == "":
        return None, "missing", None
    if not isinstance(value, str):
        return None, "malformed", None
    day_only = _DATE.fullmatch(value) is not None
    if not day_only and (
        not _INSTANT.fullmatch(value) or value.endswith("-00:00")
    ):
        return None, "malformed", None
    fraction = re.search(r"\.([0-9]+)", value)
    if fraction and any(digit != "0" for digit in fraction[1][6:]):
        # Python would silently truncate these digits across an admission boundary.
        return None, "unsupported", None
    try:
        parsed = datetime.fromisoformat(
            value + "T00:00:00+00:00" if day_only else value.upper().replace("Z", "+00:00")
        )
        return utc(parsed), "known", "day" if day_only else "instant"
    except (ValueError, OverflowError):
        return None, "malformed", None


def warning_window(date: datetime | None, observed_at: datetime) -> Window:
    now = utc(observed_at)
    if date is None:
        return "unknown"
    remaining = utc(date) - now
    if remaining <= timedelta(0):
        return "expired"
    if remaining <= timedelta(days=7):
        return "7-day"
    if remaining <= timedelta(days=30):
        return "30-day"
    if remaining <= timedelta(days=90):
        return "90-day"
    return "beyond-90-days"


def identifier(value: Any) -> str | None:
    # Do not publish arbitrary Azure strings, resource IDs, endpoints or control characters.
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def publisher_format(value: Any) -> str | None:
    # ARM formats include publisher names such as "Mistral AI" and "Black Forest Labs".
    return value if isinstance(value, str) and _FORMAT.fullmatch(value) and value == value.strip() else None


@dataclass(frozen=True)
class ModelState:
    name: str | None
    format: str | None
    version: str | None
    sku: str | None
    capacity: int | None
    version_upgrade_option: str | None
    provisioning_state: str | None

    @classmethod
    def from_record(cls, record: dict[str, Any], *, deployed: bool) -> ModelState:
        capacity = record.get("capacity")
        return cls(
            name=identifier(record.get("modelName" if deployed else "name")),
            format=publisher_format(record.get("format")),
            version=identifier(record.get("version")),
            sku=identifier(record.get("sku")),
            capacity=capacity if type(capacity) is int and 0 <= capacity <= 2**31 - 1 else None,
            version_upgrade_option=identifier(record.get("versionUpgradeOption")),
            provisioning_state=identifier(record.get("provisioningState")) if deployed else None,
        )


@dataclass(frozen=True)
class Evidence:
    source: Literal["subscription", "public"]
    field: str
    reference: str
    observed_at: str
    value: str | None
    state: DateState
    precision: str | None
    window: Window
    authoritative: bool
    qualifier: str = "exact"

    @property
    def unsafe(self) -> bool:
        if not self.authoritative or self.state != "known":
            return False
        if self.field == "model.lifecycleStatus":
            return (self.value or "").casefold() in UNSAFE_LIFECYCLE
        return self.window in {"expired", "7-day"}


@dataclass(frozen=True)
class PublicObservation:
    name: str
    format: str
    version: str
    region: str
    sku: str
    evidence: Evidence


def date_evidence(
    value: Any,
    *,
    field: str,
    observed_at: datetime,
    source: Literal["subscription", "public"] = "subscription",
    reference: str = MODELS_REFERENCE,
    qualifier: str = "exact",
    evaluated_at: datetime | None = None,
) -> Evidence:
    parsed, state, precision = parse_date(value)
    return Evidence(
        source=source,
        field=field,
        reference=reference,
        observed_at=timestamp(observed_at),
        value=timestamp(parsed) if parsed is not None else None,
        state=state,
        precision=precision,
        window=warning_window(parsed, evaluated_at or observed_at),
        authoritative=source == "subscription",
        qualifier=qualifier,
    )


def load_public_observations(path: Path | None, now: datetime) -> tuple[PublicObservation, ...]:
    """Read explicitly scoped, human-sourced evidence, never scrape or infer dates."""
    if path is None:
        return ()
    if path.stat().st_size > MAX_PUBLIC_BYTES:
        raise ValueError("Public evidence exceeds the 128 KiB input limit.")
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list) or len(records) > MAX_PUBLIC_RECORDS:
        raise ValueError("Public evidence must be an array of at most 256 observations.")
    observations = []
    fields = {"name", "format", "version", "region", "sku"}
    for record in records:
        if not isinstance(record, dict) or set(record) != fields | {
            "source_url", "observed_at", "date", "qualifier"
        }:
            raise ValueError("Public evidence has an invalid observation shape.")
        if any(
            (publisher_format(record[field]) if field == "format" else identifier(record[field])) is None
            for field in fields
        ):
            raise ValueError("Public evidence requires exact model/format/version/region/SKU identifiers.")
        url = record["source_url"]
        if not isinstance(url, str) or len(url) > 512:
            raise ValueError("Public evidence requires a bounded Microsoft Learn source URL.")
        parsed_url = urlsplit(url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.netloc != "learn.microsoft.com"
            or not parsed_url.path.startswith(("/azure/", "/en-us/azure/"))
            or parsed_url.query
            or any(char.isspace() or char in "<>\"'`|" for char in url)
        ):
            raise ValueError("Public evidence requires a public Microsoft Learn URL without credentials/query.")
        source_time, state, precision = parse_date(record["observed_at"])
        if (
            state != "known" or precision != "instant" or source_time is None
            or source_time > utc(now)
        ):
            raise ValueError("Public source observation time must be an offset-qualified, nonfuture timestamp.")
        if record["qualifier"] not in {"exact", "not-before"}:
            raise ValueError("Public date qualifier must be exact or not-before; do not infer a deadline.")
        observations.append(
            PublicObservation(
                **{field: record[field] for field in fields},
                evidence=date_evidence(
                    record["date"],
                    field="public.retirementDate",
                    observed_at=source_time,
                    evaluated_at=now,
                    source="public",
                    reference=url,
                    qualifier=record["qualifier"],
                ),
            )
        )
    return tuple(observations)


def model_evidence(
    model: ModelState,
    region: str,
    offered: Sequence[dict[str, Any]] | None,
    now: datetime,
    public: Sequence[PublicObservation] = (),
) -> tuple[Evidence, ...]:
    matching = [
        row["model"]
        for row in offered or ()
        if isinstance(row.get("model"), dict)
        and str(row["model"].get("name") or "").casefold() == (model.name or "").casefold()
        and str(row["model"].get("format") or "").casefold() == (model.format or "").casefold()
        and row["model"].get("version") == model.version
    ]
    result: list[Evidence] = []
    # Missing offering evidence is not evidence that an exact deployment stopped serving.
    for entry in matching or [{}]:
        lifecycle = identifier(entry.get("lifecycleStatus"))
        result.append(
            Evidence(
                source="subscription",
                field="model.lifecycleStatus",
                reference=MODELS_REFERENCE,
                observed_at=timestamp(now),
                value=lifecycle,
                state="known" if lifecycle else "missing",
                precision=None,
                window="unknown",
                authoritative=True,
            )
        )
        skus = [
            sku for sku in entry.get("skus") or ()
            if isinstance(sku, dict)
            and str(sku.get("name") or "").casefold() == (model.sku or "").casefold()
        ]
        for sku in skus or [{}]:
            result.append(
                date_evidence(
                    sku.get("deprecationDate"),
                    field="model.skus[].deprecationDate",
                    observed_at=now,
                )
            )
        deprecation = entry.get("deprecation")
        if isinstance(deprecation, dict) and "inference" in deprecation:
            result.append(
                date_evidence(
                    deprecation["inference"],
                    field="model.deprecation.inference",
                    observed_at=now,
                )
            )
    if offered is None:
        result = [replace(evidence, state="unavailable") for evidence in result]
    for row in public:
        if (
            row.name.casefold() == (model.name or "").casefold()
            and row.format.casefold() == (model.format or "").casefold()
            and row.version == model.version
            and row.region.casefold() == region.casefold()
            and row.sku.casefold() == (model.sku or "").casefold()
        ):
            result.append(row.evidence)
    # Identical duplicates add no evidence; contradictory duplicates remain separate.
    return tuple(dict.fromkeys(result))


def evidence_gaps(evidence: Sequence[Evidence]) -> bool:
    return any(
        item.state != "known"
        or (
            item.field == "model.lifecycleStatus"
            and (item.value or "").casefold() not in KNOWN_LIFECYCLE
        )
        for item in evidence
    )


def evidence_conflicts(evidence: Sequence[Evidence]) -> tuple[str, ...]:
    dates = {
        item.value for item in evidence
        if item.state == "known" and item.field != "model.lifecycleStatus"
        and item.qualifier == "exact"
    }
    lifecycle = {
        (item.value or "").casefold()
        for item in evidence if item.field == "model.lifecycleStatus" and item.value
    }
    conflicts = []
    if len(dates) > 1:
        conflicts.append("different scoped dates; no merged retirement date")
    if len(lifecycle) > 1:
        conflicts.append("contradictory subscription lifecycle observations")
    return tuple(conflicts)


@dataclass(frozen=True)
class RetirementObservation:
    deployment_name: str | None
    region: str | None
    observed_at: str
    inventory_state: InventoryState
    inventory_observed_at: str | None
    catalog: ModelState | None
    deployed: ModelState | None
    drift: tuple[str, ...]
    catalog_evidence: tuple[Evidence, ...]
    deployed_evidence: tuple[Evidence, ...]

    @property
    def decision(self) -> str:
        if self.catalog is None:
            return "observe-only"
        if self.inventory_state == "unavailable":
            return "unknown"
        unsafe = any(item.unsafe for item in self.catalog_evidence)
        if unsafe:
            return "block-addition-or-change" if self.drift else "reconcile-warning"
        return "no-authoritative-block"

    @property
    def incomplete(self) -> bool:
        states = [state for state in (self.catalog, self.deployed) if state is not None]
        return (
            self.inventory_state != "observed"
            or self.deployment_name is None or self.region is None
            or (self.deployed is not None and self.deployed.provisioning_state is None)
            or any(
                not all((state.name, state.format, state.version, state.sku, state.version_upgrade_option))
                or state.capacity is None
                for state in states
            )
            or evidence_gaps(self.catalog_evidence)
            or evidence_gaps(self.deployed_evidence)
        )

    @property
    def conflicts(self) -> tuple[str, ...]:
        return tuple(
            f"{label}: {conflict}"
            for label, evidence in (
                ("catalog", self.catalog_evidence), ("deployed", self.deployed_evidence)
            )
            for conflict in evidence_conflicts(evidence)
        )

    @property
    def attention(self) -> bool:
        return bool(self.drift or self.conflicts) or any(
            item.unsafe or item.window in {"expired", "7-day", "30-day", "90-day"}
            for item in (*self.catalog_evidence, *self.deployed_evidence)
        )

    def document(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "admission_decision": self.decision,
            "coverage": "unknown" if self.incomplete else "known",
            "conflicts": self.conflicts,
        }


def observe_deployment(
    required: dict[str, Any] | None,
    existing: dict[str, Any] | None,
    drift: Sequence[str],
    offered: Sequence[dict[str, Any]] | None,
    *,
    now: datetime,
    inventory_state: InventoryState,
    inventory_observed_at: datetime | None,
    public: Sequence[PublicObservation] = (),
) -> RetirementObservation:
    record = required if required is not None else existing
    if record is None:
        raise ValueError("A retirement observation needs a catalog target or deployed record.")
    catalog = ModelState.from_record(required, deployed=False) if required is not None else None
    deployed = ModelState.from_record(existing, deployed=True) if existing is not None else None
    region = identifier(record.get("region"))
    # Field names plus both allowlisted states retain drift without copying raw Azure error/data strings.
    drift_fields = tuple(
        label for label in (
            "deployment is absent", "deployment is outside the catalog", "model", "format",
            "version", "SKU", "capacity", "versionUpgradeOption", "provisioningState"
        )
        if any(item == label or item.startswith(label + " is ") for item in drift)
    )
    if inventory_state == "unavailable":
        drift_fields = ("inventory unavailable",)
    return RetirementObservation(
        deployment_name=identifier(record.get("deploymentName")),
        region=region,
        observed_at=timestamp(now),
        inventory_state=inventory_state,
        inventory_observed_at=timestamp(inventory_observed_at) if inventory_observed_at else None,
        catalog=catalog,
        deployed=deployed,
        drift=drift_fields,
        catalog_evidence=model_evidence(catalog, region or "", offered, now, public) if catalog else (),
        deployed_evidence=model_evidence(deployed, region or "", offered, now, public) if deployed else (),
    )


def observation_summary(observation: RetirementObservation) -> str:
    def state_text(state: ModelState | None) -> str:
        if state is None:
            return "absent" if observation.inventory_state == "observed" else "unknown"
        return (
            f"{state.name or 'unknown'}@{state.version or 'unknown'} "
            f"{state.sku or 'unknown'} upgrade={state.version_upgrade_option or 'unknown'}"
        )

    details = []
    for label, evidence in (
        ("catalog", observation.catalog_evidence), ("deployed", observation.deployed_evidence)
    ):
        for item in evidence:
            details.append(
                f"{label} {item.field}={item.value or item.state}"
                f" [{item.source}, {item.window}, {item.qualifier}]"
            )
    return (
        f"{observation.deployment_name or 'unknown'} ({observation.region or 'unknown'}): "
        f"catalog={state_text(observation.catalog)}; deployed={state_text(observation.deployed)}; "
        f"drift={','.join(observation.drift) or 'none'}; "
        f"coverage={'unknown' if observation.incomplete else 'known'}; "
        f"policy={observation.decision}; "
        + "; ".join(details)
        + (f"; {'; '.join(observation.conflicts)}" if observation.conflicts else "")
        + f"; observed={observation.observed_at}."
    )


@dataclass(frozen=True)
class SourceRead:
    source: str
    region: str | None
    status: Literal["observed", "unavailable", "not-requested"]
    observed_at: str
    problem: str | None = None


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"


def _cell(value: Any) -> str:
    return html.escape(str(value), quote=True).replace("|", "&#124;").replace("\n", " ")


def render_report(report: dict[str, Any]) -> str:
    lines = [
        "## Model retirement observations",
        "",
        f"Status: **{report['status']}**. Observed at {report['observed_at']}.",
        (
            f"Catalog SHA-256: `{report['catalog_sha256']}`; profile: `{report['capacity_profile']}`; "
            f"Anthropic included: `{str(report['include_anthropic']).lower()}`."
        ),
        (
            f"Coverage: {report['total_observations']} observations; "
            f"{report['unknown_observations']} unknown; {report['omitted_observations']} omitted."
        ),
        f"Public comparison: **{report['public_comparison']}**.",
        "",
        (
            f"Policy `{POLICY_VERSION}`: exact UTC 90/30/7-day windows; expired at or before "
            f"observation time. Authoritative desired-target dates within {BLOCK_WITHIN_DAYS} days "
            "or API Deprecating/Deprecated block additions/changes only. Exact Succeeded "
            "reconciles warn even when expired; this is not proof that inference still works. "
            "Date-only values use 00:00 UTC; offset-free timestamps are unknown. "
            "SKU, model-inference and public dates are not interchangeable; no date is inferred "
            "from version/launch/upgrade policy. Public evidence never authorizes or blocks deployment."
        ),
        "",
        "| Read source | Region | Status | Observation time | Problem |",
        "| --- | --- | --- | --- | --- |",
    ]
    for source in report["sources"]:
        lines.append("| " + " | ".join(_cell(source[key] or "-") for key in (
            "source", "region", "status", "observed_at", "problem"
        )) + " |")
    if not report["observations"]:
        lines.extend(["", "**No retirement coverage is available; this is not a clean inventory.**"])
    for observation in report["observations"]:
        lines.extend([
            "",
            (
                f"### {_cell(observation['deployment_name'] or 'unknown')} "
                f"({_cell(observation['region'] or 'unknown')})"
            ),
            "",
            (
                f"Admission: **{observation['admission_decision']}**; coverage: "
                f"**{observation['coverage']}**; inventory: {observation['inventory_state']} "
                f"at {observation['inventory_observed_at'] or 'unknown'}; "
                f"drift: {_cell(', '.join(observation['drift']) or 'none')}."
            ),
            "",
            "| State | Model | Format | Version | SKU | Capacity | Upgrade policy | Provisioning |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ])
        for label in ("catalog", "deployed"):
            state = observation[label]
            if state is None:
                absent = "absent" if observation["inventory_state"] == "observed" else "unknown"
                lines.append(f"| {label} | {absent} | - | - | - | - | - | - |")
            else:
                lines.append("| " + label + " | " + " | ".join(_cell(
                    state[key] if state[key] is not None else "unknown"
                ) for key in (
                    "name", "format", "version", "sku", "capacity",
                    "version_upgrade_option", "provisioning_state"
                )) + " |")
        lines.extend([
            "",
            "| State | Field | Value (UTC) | Precision / qualifier | Window | Source / authority | Source observed at |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ])
        for label in ("catalog", "deployed"):
            for evidence in observation[label + "_evidence"]:
                lines.append("| " + " | ".join(_cell(value) for value in (
                    label, evidence["field"], evidence["value"] or evidence["state"],
                    f"{evidence['precision'] or '-'} / {evidence['qualifier']}",
                    evidence["window"],
                    f"{evidence['source']} / {evidence['authoritative']}",
                    evidence["observed_at"],
                )) + " |")
        for conflict in observation["conflicts"]:
            lines.append(f"\n**Different evidence:** {_cell(conflict)}.")
    references = sorted({
        evidence["reference"]
        for observation in report["observations"]
        for key in ("catalog_evidence", "deployed_evidence")
        for evidence in observation[key]
    })
    if references:
        lines.extend(["", "Source references: " + " ".join(f"<{url}>" for url in references)])
    lines.extend(["", "Read-only evidence, not migration approval. No catalog or Azure state was changed.", ""])
    return "\n".join(lines)


def build_report(
    observations: Sequence[RetirementObservation],
    sources: Sequence[SourceRead],
    *,
    now: datetime,
    catalog_bytes: bytes,
    capacity_profile: str,
    include_anthropic: bool,
    public_count: int,
) -> dict[str, Any]:
    total = len(observations)
    unknown = sum(item.incomplete for item in observations)
    incomplete = not total or unknown > 0 or any(source.status != "observed" for source in sources)
    report = {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "observed_at": timestamp(now),
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "capacity_profile": capacity_profile,
        "include_anthropic": include_anthropic,
        "public_comparison": (
            "unavailable" if any(
                source.source == "public-evidence-file" and source.status != "observed"
                for source in sources
            ) else "supplied" if public_count else (
                "supplied-empty" if any(source.source == "public-evidence-file" for source in sources)
                else "not-supplied"
            )
        ),
        "status": "incomplete" if incomplete else (
            "attention" if any(item.attention for item in observations) else "clear"
        ),
        "total_observations": total,
        "unknown_observations": unknown,
        "omitted_observations": max(0, total - MAX_OBSERVATIONS),
        "sources": [asdict(source) for source in sources],
        "observations": [item.document() for item in observations[:MAX_OBSERVATIONS]],
    }
    if report["omitted_observations"]:
        report["status"] = "incomplete"
    while (
        len(report_json(report).encode("utf-8")) > MAX_REPORT_BYTES
        or len(render_report(report).encode("utf-8")) > MAX_MARKDOWN_BYTES
    ):
        if not report["observations"]:
            raise ValueError("Retirement source metadata exceeds the report size budget.")
        report["observations"].pop()
        report["omitted_observations"] += 1
        report["status"] = "incomplete"
    return report


def replace_docs_section(document: str, generated: str) -> str:
    if document.count(DOC_START) != 1 or document.count(DOC_END) != 1:
        raise ValueError("Retirement documentation must have exactly one start/end marker pair.")
    before, rest = document.split(DOC_START)
    if DOC_END not in rest:
        raise ValueError("Retirement documentation markers are out of order.")
    _, after = rest.split(DOC_END)
    return before + DOC_START + "\n" + generated + DOC_END + after


def write_report(directory: Path, report: dict[str, Any], documentation: str) -> int:
    rendered = render_report(report)
    preview = replace_docs_section(documentation, rendered)
    serialized = report_json(report)
    if (
        len(serialized.encode("utf-8")) > MAX_REPORT_BYTES
        or len(rendered.encode("utf-8")) > MAX_MARKDOWN_BYTES
        or len(preview.encode("utf-8")) > MAX_DOC_PREVIEW_BYTES
    ):
        raise ValueError("Retirement report exceeds its serialized size budget.")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model-retirements.json").write_text(serialized, encoding="utf-8")
    (directory / "model-retirements.md").write_text(rendered, encoding="utf-8")
    (directory / "region-capability-matrix.md").write_text(preview, encoding="utf-8")
    return {"clear": 0, "attention": 1, "incomplete": 2}[report["status"]]
