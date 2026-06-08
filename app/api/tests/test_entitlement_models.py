"""Entitlement model invariants: unlimited detection + ge=0 validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai4ia_api.entitlements.models import (
    Entitlement,
    EntitlementDecision,
    EntitlementLimits,
)


def test_default_limits_are_unlimited():
    limits = EntitlementLimits()
    assert limits.is_unlimited is True
    assert limits.has_any_limit is False
    assert limits.disabled is False


def test_disabled_is_not_unlimited_but_has_no_numeric_limit():
    limits = EntitlementLimits(disabled=True)
    # disabled is deliberately excluded from has_any_limit so the unlimited fast
    # path only short-circuits a not-disabled user.
    assert limits.has_any_limit is False
    assert limits.is_unlimited is False


@pytest.mark.parametrize(
    "field",
    [
        "requestsPerMinute",
        "tokensPerDay",
        "costPerDayMicroUsd",
        "tokensPerMonth",
        "costPerMonthMicroUsd",
    ],
)
def test_any_single_limit_marks_not_unlimited(field):
    limits = EntitlementLimits(**{field: 5})
    assert limits.has_any_limit is True
    assert limits.is_unlimited is False


@pytest.mark.parametrize(
    "field",
    [
        "requestsPerMinute",
        "tokensPerDay",
        "costPerDayMicroUsd",
        "tokensPerMonth",
        "costPerMonthMicroUsd",
    ],
)
def test_zero_is_a_valid_hard_block(field):
    limits = EntitlementLimits(**{field: 0})
    assert limits.has_any_limit is True
    assert limits.is_unlimited is False


@pytest.mark.parametrize(
    "field",
    [
        "requestsPerMinute",
        "tokensPerDay",
        "costPerDayMicroUsd",
        "tokensPerMonth",
        "costPerMonthMicroUsd",
    ],
)
def test_negative_limit_is_rejected(field):
    with pytest.raises(ValidationError):
        EntitlementLimits(**{field: -1})


def test_entitlement_unlimited_classmethod():
    ent = Entitlement.unlimited("u-123")
    assert ent.id == "u-123"
    assert ent.userId == "u-123"
    assert ent.is_unlimited is True


def test_decision_allow_helper():
    decision = EntitlementDecision.allow()
    assert decision.allowed is True
    assert decision.code == 200
    assert decision.retry_after_seconds is None
