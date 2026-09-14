"""Bounded predecessor state; missing history is not a zero-failure observation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from .configuration import Configuration
from .contracts import (
    CanaryError, INTERVAL_SECONDS, MAX_RUNS, Report, Run, SHA256, THRESHOLD,
    VERSION, integer, obj, timestamp,
)


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

    def document(self) -> dict[str, Any]:
        data = asdict(self)
        self.parse(data)
        return data

    @classmethod
    def parse(cls, value: Any) -> State:
        data = obj(value, set(cls.__dataclass_fields__)).copy()
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
        if type(data["blocked"]) is not bool or data["control"] not in ("disabled", "bootstrap", "observe", "blocked"):
            raise CanaryError("state_invalid")
        if data["last_attempt_at"] is not None:
            attempted = timestamp(data["last_attempt_at"])
            if attempted > timestamp(data["report"].observed_at):
                raise CanaryError("state_invalid")
        if data["observations"] > 0 and data["last_attempt_at"] is None:
            raise CanaryError("state_invalid")
        if not data["report"].cleanup_safe and not data["blocked"]:
            raise CanaryError("state_invalid")
        if data["control"] in ("disabled", "bootstrap") and (
            data["report"].chat_attempts or data["report"].realtime_attempts
            or data["report"].coverage != "unscored"
        ):
            raise CanaryError("state_invalid")
        return cls(**data)


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
    control: str, attempted: bool = False,
) -> State:
    if control == "bootstrap":
        return State(
            report, config.scope_digest if config else None, config.approval_digest if config else None,
            previous.report.run.run_id if previous else None, 0,
            previous.last_attempt_at if previous else None, False, control,
            chat=(previous.chat if previous else Counter()).advance("unknown"),
            realtime=(previous.realtime if previous else Counter()).advance("unknown"),
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
