"""Catalog-owned deployment naming shared by capacity tools."""

from __future__ import annotations

from typing import Any

PATTERN = "{model}-{subscriptionToken}-{region}-{skuShort}"


def deployment_name(models: dict[str, Any], model: str, deployment: dict[str, Any]) -> str:
    naming = models["naming"]
    return str(naming.get("pattern") or PATTERN).format(
        model=model,
        subscriptionToken=naming["subscriptionToken"],
        region=deployment["region"],
        skuShort=naming["skuShort"][deployment["sku"]],
    )
