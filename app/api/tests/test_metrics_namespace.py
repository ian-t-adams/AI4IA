"""Unit tests for the ARM-resource-id -> Azure Monitor metric-namespace helper.

``_metric_namespace`` is pure (no Azure SDK import), so these run without
``azure-monitor-querymetrics`` installed and pin the namespace derivation the batch
metrics client relies on for each admin-dashboard resource type.
"""
from __future__ import annotations

import pytest

from ai4ia_api.metrics.azure_monitor import _metric_namespace

_SUB = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"


@pytest.mark.parametrize(
    ("resource_id", "expected"),
    [
        (
            f"{_SUB}/providers/Microsoft.DocumentDB/databaseAccounts/cosmos-x",
            "Microsoft.DocumentDB/databaseAccounts",
        ),
        (
            f"{_SUB}/providers/Microsoft.App/containerApps/ca-api",
            "Microsoft.App/containerApps",
        ),
        (
            f"{_SUB}/providers/Microsoft.Search/searchServices/search-x",
            "Microsoft.Search/searchServices",
        ),
        (
            f"{_SUB}/providers/Microsoft.DBforPostgreSQL/flexibleServers/pg-x",
            "Microsoft.DBforPostgreSQL/flexibleServers",
        ),
    ],
)
def test_metric_namespace_parses_resource_types(resource_id: str, expected: str) -> None:
    assert _metric_namespace(resource_id) == expected


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "not-a-resource-id",
        # No '/providers/' segment at all.
        "/subscriptions/x/resourceGroups/rg/Microsoft.App/containerApps/ca",
        # Only one segment after '/providers/' -> can't form '<Provider>/<type>'.
        "/subscriptions/x/providers/Microsoft.App",
    ],
)
def test_metric_namespace_rejects_malformed(bad_id: str) -> None:
    with pytest.raises(ValueError):
        _metric_namespace(bad_id)
