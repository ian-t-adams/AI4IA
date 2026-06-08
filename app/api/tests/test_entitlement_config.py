"""Startup invariants for entitlement enforcement (Settings.validate_runtime)."""
from __future__ import annotations

import pytest

from tests.conftest import make_settings


def test_enforcement_requires_usage_metering():
    # Enabling enforcement while metering is off would let positive budgets
    # silently never trip; the guard must refuse it.
    settings = make_settings(entitlements_enabled=True, usage_metering_enabled=False)
    with pytest.raises(RuntimeError, match="usage metering"):
        settings.validate_runtime()


def test_enforcement_off_with_metering_off_is_allowed():
    settings = make_settings(entitlements_enabled=False, usage_metering_enabled=False)
    settings.validate_runtime()  # must not raise


def test_default_posture_validates():
    # Shipped defaults: both enabled -> valid.
    make_settings().validate_runtime()
