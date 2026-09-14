from datetime import date, datetime, time, timezone
import hashlib
import os
from pathlib import Path
from zoneinfo import TZPATH

import pytest
from pydantic import ValidationError

from ai4ia_api.workflows.automation_common import AutomationError, ExecutionLimits, exact_arguments
from ai4ia_api.workflows.scheduling import (
    ScheduleRule, first_occurrence, next_occurrence, occurrence, zone_identity,
)


def rule(**changes):
    return ScheduleRule.model_validate({
        "frequency": "daily", "timezone": "America/New_York",
        "localTime": "02:30", "maxOccurrences": 10, **changes,
    })


def test_gap_skips_slot_but_normal_day_runs():
    schedule = rule()
    assert occurrence(schedule, date(2026, 3, 8)) is None
    normal = occurrence(schedule, date(2026, 3, 9))
    assert normal is not None
    assert normal.dueAt == datetime(2026, 3, 9, 6, 30, tzinfo=timezone.utc)
    following = next_occurrence(schedule, datetime(2026, 3, 8, tzinfo=timezone.utc))
    assert following == normal


def test_fold_runs_only_first_occurrence_even_between_fold_instants():
    schedule = rule(localTime="01:30")
    first = occurrence(schedule, date(2026, 11, 1))
    assert first is not None
    assert first.dueAt == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    after_first = next_occurrence(schedule, first.dueAt)
    assert after_first is not None
    assert after_first.localSlot == "2026-11-02T01:30"
    assert after_first.dueAt == datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)


def test_half_hour_gap_and_skipped_calendar_day():
    lord_howe = rule(timezone="Australia/Lord_Howe", localTime="02:15")
    assert occurrence(lord_howe, date(2026, 10, 4)) is None
    assert occurrence(lord_howe, date(2026, 10, 5)) is not None
    apia = rule(timezone="Pacific/Apia", localTime="12:00")
    assert occurrence(apia, date(2011, 12, 30)) is None
    assert occurrence(apia, date(2011, 12, 31)) is not None


def test_weekly_keeps_wall_clock_across_year_boundary():
    schedule = rule(frequency="weekly", weekday=4, localTime="09:00")
    after = datetime(2026, 12, 31, tzinfo=timezone.utc)
    upcoming = next_occurrence(schedule, after)
    assert upcoming is not None
    assert upcoming.localSlot == "2027-01-01T09:00"
    assert upcoming.dueAt == datetime(2027, 1, 1, 14, tzinfo=timezone.utc)


def test_once_has_one_future_instant_and_gap_refuses():
    schedule = rule(frequency="once", localDate="2026-11-01", localTime="01:30", maxOccurrences=1)
    before = datetime(2026, 10, 31, tzinfo=timezone.utc)
    first = first_occurrence(schedule, before)
    assert next_occurrence(schedule, first.dueAt) is None
    gap = rule(frequency="once", localDate="2026-03-08", maxOccurrences=1)
    with pytest.raises(AutomationError, match="no future valid"):
        first_occurrence(gap, datetime(2026, 3, 1, tzinfo=timezone.utc))


def test_rule_identity_and_monotonic_slot_fence():
    schedule = rule(timezone="UTC")
    _, identity = zone_identity("UTC")
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert next_occurrence(schedule, now, expected_zone_digest=identity) is not None
    with pytest.raises(AutomationError, match="rules changed"):
        next_occurrence(schedule, now, expected_zone_digest="0" * 64)
    next_slot = next_occurrence(schedule, now, after_slot="2026-09-15T02:30")
    assert next_slot is not None and next_slot.localSlot == "2026-09-16T02:30"


@pytest.mark.parametrize("changes", [
    {"frequency": "weekly"}, {"frequency": "once", "localDate": "2026-12-01"},
    {"localTime": "02:30:01"}, {"localTime": "02:30+01:00"},
    {"timezone": "../UTC"}, {"timezone": "/etc/passwd"},
    {"maxOccurrences": True}, {"maxOccurrences": 367}, {"missedPolicy": "backfill"},
])
def test_unsupported_rules_are_rejected(changes):
    with pytest.raises(ValidationError):
        rule(**changes)


def test_bad_or_naive_time_is_not_guessed():
    with pytest.raises(AutomationError, match="unavailable"):
        first_occurrence(rule(timezone="Not/A_Zone"), datetime.now(timezone.utc))
    with pytest.raises(AutomationError, match="explicit timezone"):
        first_occurrence(rule(), datetime(2026, 1, 1))
    assert rule().localTime == time(2, 30)


def test_exact_arguments_preserve_nested_whitespace_and_identity():
    args, identity, shown = exact_arguments('{"to":"hello@example.org","body":{"text":"a\\n b"}}')
    assert args["body"]["text"] == "a\n b"
    assert exact_arguments(shown)[1] == identity
    assert exact_arguments('{"body":{"text":"a\\n b"},"to":"hello@example.org"}')[1] == identity
    assert exact_arguments('{"to":"elsewhere@example.org","body":{"text":"a\\n b"}}')[1] != identity


@pytest.mark.parametrize("raw", [
    '{"to":"safe","to":"evil"}', '{"x":NaN}', '[]',
    '{"authorization":"short-secret"}', '{"url":"https://host.example/?sig=short"}',
    '{"x":"' + "x" * 8200 + '"}', '{"x":' + "[" * 20 + "0" + "]" * 20 + "}",
])
def test_inexact_or_secret_arguments_cannot_be_approved(raw):
    with pytest.raises(AutomationError):
        exact_arguments(raw)


def test_explicit_uncapped_spend_does_not_enable_a_dollar_guarantee():
    assert ExecutionLimits(spendMode="no_hard_dollar_cap").maxApplicationDispatches == 64
    with pytest.raises(ValidationError):
        ExecutionLimits()
    with pytest.raises(ValidationError):
        ExecutionLimits(spendMode="no_hard_dollar_cap", maxSpendMicroUsd=1)
    with pytest.raises(ValidationError):
        ExecutionLimits(spendMode="hard_cap")


def system_tzif(name):
    roots = os.environ["PYTHONTZPATH"].split(os.pathsep) if "PYTHONTZPATH" in os.environ else TZPATH
    for root in roots:
        path = Path(root).joinpath(*name.split("/"))
        if path.is_file():
            return path.read_bytes()
    pytest.fail("Real IANA TZif test data is required; set PYTHONTZPATH on Windows.")


def test_zone_uses_exact_hashed_bytes_and_refuses_changed_or_removed_rules(monkeypatch, tmp_path):
    raw = system_tzif("UTC")
    alternative = system_tzif("America/New_York")
    selected = tmp_path / "Selected"
    selected.write_bytes(raw)
    monkeypatch.setenv("PYTHONTZPATH", str(tmp_path))
    version, identity = zone_identity("Selected")
    assert version == "system-tzif"
    assert identity == hashlib.sha256(b"Selected\0" + raw).hexdigest()
    schedule = rule(timezone="Selected")
    moment = datetime(2026, 9, 10, tzinfo=timezone.utc)
    first = next_occurrence(schedule, moment, expected_zone_digest=identity)
    assert first.dueAt.hour == 2
    selected.write_bytes(alternative)
    with pytest.raises(AutomationError, match="rules changed"):
        next_occurrence(schedule, moment, expected_zone_digest=identity)
    assert next_occurrence(schedule, moment).dueAt.hour == 6
    selected.unlink()
    with pytest.raises(AutomationError, match="unavailable"):
        next_occurrence(schedule, moment, expected_zone_digest=identity)


def test_untrusted_zone_paths_cannot_leave_the_configured_root(monkeypatch, tmp_path):
    raw = system_tzif("UTC")
    root = tmp_path / "trusted"
    root.mkdir()
    (root / "UTC").write_bytes(raw)
    outside = tmp_path / "outside"
    outside.write_bytes(raw)
    original = Path.resolve

    def redirected(self, *args, **kwargs):
        return outside if self == root / "Escape" else original(self, *args, **kwargs)

    monkeypatch.setenv("PYTHONTZPATH", str(root))
    monkeypatch.setattr(Path, "resolve", redirected)
    assert zone_identity("UTC")[1]
    with pytest.raises(AutomationError, match="leaves its trusted"):
        zone_identity("Escape")
    with pytest.raises(AutomationError, match="IANA timezone"):
        zone_identity("../outside")
    monkeypatch.setenv("PYTHONTZPATH", "relative-directory")
    with pytest.raises(AutomationError, match="absolute trusted"):
        zone_identity("UTC")
