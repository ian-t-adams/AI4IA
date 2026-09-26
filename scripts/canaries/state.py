"""Bounded predecessor state; missing history is not a zero-failure observation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from .configuration import Configuration
from .contracts import (
    CanaryError, INTERVAL_SECONDS, MAX_RUNS, Report, Run, SHA256, THRESHOLD,
    VERSION, integer, obj, timestamp,
)
from .resolution import EVIDENCE, MAX_LOST_RUNS, Attested, Resolution


@dataclass(frozen=True)
class Counter:
    failures: int | None = None
    alerting: bool = False
    transition: str = "none"

    def advance(self, outcome: str) -> Counter:
        if outcome == "pass":
            return Counter(0, False, "recovered" if self.alerting else "none")
        if outcome == "fail":
            count = min(THRESHOLD, (self.failures or 0) + 1)
            active = self.alerting or count >= THRESHOLD
            return Counter(count, active, "firing" if active and not self.alerting else "none")
        # An observation gap interrupts consecutiveness, not an existing alert.
        return Counter(None, self.alerting, "none")

    @classmethod
    def parse(cls, value: Any) -> Counter:
        raw = obj(value, set(cls.__dataclass_fields__))
        if raw["failures"] is not None:
            integer(raw["failures"], 0, THRESHOLD)
        if type(raw["alerting"]) is not bool or raw["transition"] not in ("none", "firing", "recovered"):
            raise CanaryError("state_invalid")
        result = cls(**raw)
        if (
            (result.transition == "firing" and (not result.alerting or result.failures != THRESHOLD))
            or (result.transition == "recovered" and (result.alerting or result.failures != 0))
            or (result.failures == 0 and result.alerting)
            or (result.failures == THRESHOLD and not result.alerting)
        ):
            raise CanaryError("state_invalid")
        return result


@dataclass(frozen=True)
class State:
    report: Report
    scope_digest: str | None
    approval_digest: str | None
    previous_run_id: int | None
    observations: int
    last_attempt_at: str | None
    blocked: bool
    control: str
    chat: Counter = Counter()
    realtime: Counter = Counter()
    version: int = VERSION
    # Only the owner-attested `resolved` control carries it; nothing inherits it.
    resolution: dict[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        data = asdict(self)
        if data["resolution"] is None:
            # Keep every non-resolved state readable by the previous schema, so a
            # revert of this code cannot block a chain that never resolved.
            del data["resolution"]
        self.parse(data)
        return data

    @classmethod
    def parse(cls, value: Any) -> State:
        fields = set(cls.__dataclass_fields__)
        data = obj(value).copy()
        # States written before resolution existed omit the field; they are never resolved ones.
        if set(data) not in (fields, fields - {"resolution"}):
            raise CanaryError("invalid_response")
        data.setdefault("resolution", None)
        data["report"] = Report.parse(data["report"])
        data["chat"] = Counter.parse(data["chat"])
        data["realtime"] = Counter.parse(data["realtime"])
        if type(data["version"]) is not int or data["version"] != VERSION:
            raise CanaryError("state_invalid")
        for key in ("scope_digest", "approval_digest"):
            if data[key] is not None and (
                not isinstance(data[key], str) or not SHA256.fullmatch(data[key])
            ):
                raise CanaryError("state_invalid")
        if data["previous_run_id"] is not None:
            integer(data["previous_run_id"], 1, 2**63 - 1)
            if data["previous_run_id"] == data["report"].run.run_id:
                raise CanaryError("state_invalid")
        integer(data["observations"], 0, MAX_RUNS)
        if type(data["blocked"]) is not bool or data["control"] not in (
            "disabled", "bootstrap", "observe", "blocked", "resolved",
        ):
            raise CanaryError("state_invalid")
        if data["last_attempt_at"] is not None:
            attempted = timestamp(data["last_attempt_at"])
            if attempted > timestamp(data["report"].observed_at):
                raise CanaryError("state_invalid")
        if data["observations"] > 0 and data["last_attempt_at"] is None:
            raise CanaryError("state_invalid")
        if not data["report"].cleanup_safe and not data["blocked"]:
            raise CanaryError("state_invalid")
        if data["control"] in ("disabled", "bootstrap", "resolved") and (
            data["report"].chat_attempts or data["report"].realtime_attempts
            or data["report"].coverage != "unscored"
        ):
            raise CanaryError("state_invalid")
        if data["control"] == "resolved":
            if (
                data["blocked"] or data["observations"] or data["previous_run_id"] is None
                or data["scope_digest"] is None or data["approval_digest"] is None
            ):
                raise CanaryError("state_invalid")
            _resolution(data["resolution"], data["previous_run_id"])
        elif data["resolution"] is not None:
            raise CanaryError("state_invalid")
        return cls(**data)


def _resolution(value: Any, blocked_run_id: int) -> None:
    data = obj(value, {"sha256", "blocked_run_id", "lost_run_ids", "evidence"})
    lost = data["lost_run_ids"]
    if (
        not isinstance(data["sha256"], str) or not SHA256.fullmatch(data["sha256"])
        or type(data["blocked_run_id"]) is not int or data["blocked_run_id"] != blocked_run_id
        or not isinstance(lost, list) or not 1 <= len(lost) <= MAX_LOST_RUNS
        or any(type(item) is not int or not 1 <= item < blocked_run_id for item in lost)
        or lost != sorted(set(lost)) or data["evidence"] not in EVIDENCE
    ):
        raise CanaryError("state_invalid")


def validate_predecessor(previous: State, current: Run, now: datetime) -> None:
    previous.document()
    before = previous.report.run
    if (
        before.repository != current.repository or before.repository_id != current.repository_id
        or before.number + 1 != current.number or before.run_id >= current.run_id
        or before.attempt != 1
    ):
        raise CanaryError("state_invalid")
    age = now - timestamp(previous.report.observed_at)
    if age < timedelta(0) or age > timedelta(seconds=INTERVAL_SECONDS * 3):
        raise CanaryError("state_stale")


def admit(
    config: Configuration, run: Run, previous: State | None, now: datetime, *, bootstrap: bool,
) -> None:
    if previous is None:
        if bootstrap and run.number == 1:
            return
        raise CanaryError("state_missing")
    validate_predecessor(previous, run, now)
    if previous.blocked or not previous.report.cleanup_safe:
        raise CanaryError("state_blocked")
    if bootstrap:
        # A changed approval is a new finite lease, not an automatic retry or
        # remediation of an unresolved write. The bootstrap itself is unscored.
        if previous.approval_digest == config.approval_digest and previous.observations:
            raise CanaryError("state_invalid")
        return
    if previous.control == "disabled" or previous.scope_digest != config.scope_digest:
        raise CanaryError("scope_changed")
    if previous.observations >= config.approved_runs:
        raise CanaryError("lease_exhausted")
    if previous.last_attempt_at is not None and (
        now - timestamp(previous.last_attempt_at)
    ).total_seconds() < config.interval_seconds:
        raise CanaryError("cadence")


@dataclass(frozen=True)
class Resolved:
    document: dict[str, Any]
    last_attempt_at: str


def resolve(
    config: Configuration, record: Resolution, attested: Sequence[Attested] | None,
    run: Run, previous: State | None, now: datetime, approval: str,
) -> Resolved:
    """The only transition out of a blocked chain: an attested bootstrap of a new lease.

    The resolution's own refusals never poison a chain: a blocked chain stays
    blocked and an unblocked one is unaffected by a mistaken dispatch. Only a
    predecessor that already fails validation keeps its existing poisoning code.
    """
    if previous is None:
        raise CanaryError("not_blocked")
    validate_predecessor(previous, run, now)
    if not previous.blocked:
        raise CanaryError("not_blocked")
    if approval != record.sha256:
        raise CanaryError("resolution_unapproved")
    if record.blocked_run_id != previous.report.run.run_id:
        raise CanaryError("resolution_stale")
    if (
        record.approval_id != config.approval_id
        or (previous.approval_digest is not None
            and previous.approval_digest not in record.superseded_approval_digests)
    ):
        # A resolution admits a new lease; it never renews or resets a used one.
        # The record's own new approval can never be superseded (`Resolution.load`).
        raise CanaryError("resolution_invalid")
    rows = list(attested or ())
    if (
        attested is None or sorted(row.run.run_id for row in rows) != list(record.lost_run_ids)
        or any(
            row.run.repository != run.repository or row.run.repository_id != run.repository_id
            or row.run.number >= previous.report.run.number
            or timestamp(row.updated_at) > now
            for row in rows
        )
    ):
        raise CanaryError("resolution_invalid")
    # Cadence runs from the latest investigated attempt, so a resolution can
    # never shorten the interval after a lost observation.
    anchors = [row.updated_at for row in rows]
    if previous.last_attempt_at is not None:
        anchors.append(previous.last_attempt_at)
    return Resolved(
        {
            "sha256": record.sha256, "blocked_run_id": record.blocked_run_id,
            "lost_run_ids": list(record.lost_run_ids), "evidence": record.evidence,
        },
        max(anchors, key=timestamp),
    )


def chat_outcome(report: Report) -> str:
    measured = [
        report.stages[name].outcome for name in
        ("platform", "auth", "catalog", "posture", "session", "gateway", "model", "persistence")
    ]
    if "fail" in measured or report.stages["cleanup"].outcome == "fail":
        return "fail"
    if all(value == "pass" for value in measured) and report.cleanup_safe:
        return "pass"
    return "unknown"


def finish(
    report: Report, config: Configuration | None, previous: State | None, *,
    control: str, attempted: bool = False, resolved: Resolved | None = None,
) -> State:
    if (control == "resolved") != (resolved is not None) or (resolved and (config is None or previous is None)):
        raise ValueError("Only an admitted resolution produces a resolved state.")
    if control in ("bootstrap", "resolved"):
        return State(
            report, config.scope_digest if config else None, config.approval_digest if config else None,
            previous.report.run.run_id if previous else None, 0,
            resolved.last_attempt_at if resolved else (previous.last_attempt_at if previous else None),
            False, control,
            chat=(previous.chat if previous else Counter()).advance("unknown"),
            realtime=(previous.realtime if previous else Counter()).advance("unknown"),
            resolution=resolved.document if resolved else None,
        )
    return State(
        report=report,
        scope_digest=config.scope_digest if attempted and config else (previous.scope_digest if previous else None),
        approval_digest=config.approval_digest if attempted and config else (previous.approval_digest if previous else None),
        previous_run_id=previous.report.run.run_id if previous else None,
        observations=min(MAX_RUNS, (previous.observations if previous else 0) + int(attempted)),
        last_attempt_at=report.observed_at if attempted else (previous.last_attempt_at if previous else None),
        blocked=not report.cleanup_safe or bool(previous and previous.blocked),
        control=control,
        chat=(previous.chat if previous else Counter()).advance(chat_outcome(report)),
        realtime=(previous.realtime if previous else Counter()).advance(report.stages["realtime"].outcome),
    )
