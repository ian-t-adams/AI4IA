"""Bounded wall-clock schedules using an explicitly identified IANA ruleset."""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import TZPATH, ZoneInfo

from pydantic import Field, model_validator

from .automation_common import AutomationError, AutomationModel, SHA256_PATTERN, utc

_ZONE_NAME = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")


class ScheduleRule(AutomationModel):
    frequency: Literal["once", "daily", "weekly"]
    timezone: str = Field(min_length=1, max_length=128)
    localTime: time
    localDate: date | None = None
    weekday: int | None = Field(default=None, ge=0, le=6, strict=True)
    maxOccurrences: int = Field(ge=1, le=366, strict=True)
    gapPolicy: Literal["skip"] = "skip"
    foldPolicy: Literal["first"] = "first"
    missedPolicy: Literal["skip"] = "skip"
    overlapPolicy: Literal["deny"] = "deny"

    @model_validator(mode="after")
    def validate_rule(self) -> ScheduleRule:
        if (
            self.localTime.tzinfo is not None
            or self.localTime.second
            or self.localTime.microsecond
        ):
            raise ValueError("localTime must be an offset-free hour and minute.")
        if not _ZONE_NAME.fullmatch(self.timezone):
            raise ValueError("timezone must be an IANA timezone name.")
        if self.frequency == "once":
            if self.localDate is None or self.weekday is not None or self.maxOccurrences != 1:
                raise ValueError("A once schedule requires localDate, no weekday, and one occurrence.")
        elif self.localDate is not None:
            raise ValueError("localDate is only valid for a once schedule.")
        if (self.frequency == "weekly") != (self.weekday is not None):
            raise ValueError("weekday (Monday=0) is required only for weekly schedules.")
        return self


class ScheduleOccurrence(AutomationModel):
    localSlot: str
    dueAt: datetime
    zoneVersion: str
    zoneDigest: str = Field(pattern=SHA256_PATTERN)


def _zone(name: str) -> tuple[ZoneInfo, str, str]:
    if not _ZONE_NAME.fullmatch(name) or len(name) > 128:
        raise AutomationError("invalid_timezone", "An IANA timezone name is required.", status=422)
    configured = os.environ.get("PYTHONTZPATH")
    roots = configured.split(os.pathsep) if configured is not None else TZPATH
    if any(not root or not Path(root).is_absolute() for root in roots):
        raise AutomationError("invalid_timezone_root", "PYTHONTZPATH must name absolute trusted TZif directories.")
    for root in roots:
        try:
            trusted = Path(root).resolve(strict=True)
            if not trusted.is_dir():
                raise AutomationError("invalid_timezone_root", "The configured timezone root is not a directory.")
            requested = trusted.joinpath(*name.split("/"))
            path = requested.resolve(strict=True)
            if not path.is_relative_to(trusted):
                raise AutomationError("invalid_timezone", "The timezone path leaves its trusted data root.", status=422)
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > 65536:
                    raise AutomationError("invalid_timezone", "The timezone rules are unsupported.", status=422)
                raw = stream.read(65537)
                after = os.fstat(stream.fileno())
            if (
                requested.resolve(strict=True) != path
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise AutomationError("timezone_changed", "Timezone rules changed during the read.")
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, AutomationError):
                raise
            raise AutomationError("invalid_timezone", "The system timezone rules cannot be read.", status=422) from exc
        if len(raw) > 65536 or not raw.startswith(b"TZif"):
            raise AutomationError("invalid_timezone", "The timezone rules are unsupported.", status=422)
        identity = hashlib.sha256(name.encode("ascii") + b"\0" + raw).hexdigest()
        try:
            zone = ZoneInfo.from_file(io.BytesIO(raw), key=name)
        except (ValueError, EOFError) as exc:
            raise AutomationError("invalid_timezone", "The system TZif data is malformed.", status=422) from exc
        return zone, "system-tzif", identity
    raise AutomationError(
        "invalid_timezone",
        "This IANA timezone is unavailable. Configure trusted system TZif directories with PYTHONTZPATH.",
        status=422,
    )


def require_timezone_data() -> None:
    _zone("UTC")


def zone_identity(name: str) -> tuple[str, str]:
    _, version, identity = _zone(name)
    return version, identity


def occurrence(
    rule: ScheduleRule, day: date, *, expected_zone_digest: str | None = None,
) -> ScheduleOccurrence | None:
    zone, version, identity = _zone(rule.timezone)
    if expected_zone_digest is not None and identity != expected_zone_digest:
        raise AutomationError("timezone_changed", "Timezone rules changed; review this schedule.")
    if rule.frequency == "once" and day != rule.localDate:
        return None
    if rule.frequency == "weekly" and day.weekday() != rule.weekday:
        return None
    local = datetime.combine(day, rule.localTime)
    candidates: set[datetime] = set()
    for fold in (0, 1):
        candidate = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if candidate.astimezone(zone).replace(tzinfo=None) == local:
            candidates.add(candidate)
    if not candidates:
        return None
    return ScheduleOccurrence(
        localSlot=local.isoformat(timespec="minutes"), dueAt=min(candidates),
        zoneVersion=version, zoneDigest=identity,
    )


def next_occurrence(
    rule: ScheduleRule, after: datetime, *, after_slot: str | None = None,
    expected_zone_digest: str | None = None,
) -> ScheduleOccurrence | None:
    moment = utc(after)
    zone, _, identity = _zone(rule.timezone)
    if expected_zone_digest is not None and identity != expected_zone_digest:
        raise AutomationError("timezone_changed", "Timezone rules changed; review this schedule.")
    first = rule.localDate if rule.frequency == "once" else moment.astimezone(zone).date()
    if first is None:
        raise AutomationError("invalid_schedule", "The schedule has no local date.", status=422)
    for offset in range(1 if rule.frequency == "once" else 370):
        try:
            day = first + timedelta(days=offset)
        except OverflowError:
            return None
        candidate = occurrence(rule, day, expected_zone_digest=identity)
        if candidate and candidate.dueAt > moment and (
            after_slot is None or candidate.localSlot > after_slot
        ):
            return candidate
    return None


def first_occurrence(rule: ScheduleRule, now: datetime) -> ScheduleOccurrence:
    candidate = next_occurrence(rule, now)
    if candidate is None:
        raise AutomationError(
            "invalid_schedule", "The schedule has no future valid occurrence; check the local time.",
            status=422,
        )
    return candidate
