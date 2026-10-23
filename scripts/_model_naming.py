"""Catalog-owned deployment naming shared by capacity tools."""

from __future__ import annotations

import re
from typing import Any

PATTERN = "{model}-{subscriptionToken}-{region}-{skuShort}"


def deployment_base(model: dict[str, Any]) -> str:
    """Keep provider identity separate from the Azure deployment resource token."""
    value = model.get("deploymentNameBase", model["name"])
    if "deploymentNameBase" in model:
        if (
            model.get("category") != "decision" or model.get("api") != "decision"
            or model.get("format") != "Microsoft" or not isinstance(value, str)
            or re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value) is None
            or "microsoft" in value
        ):
            raise ValueError("deploymentNameBase requires an Azure-safe, lowercase Decision resource token")
    elif model.get("category") == "decision":
        raise ValueError("Decision requires deploymentNameBase; the provider name contains an Azure reserved word")
    return value


def deployment_name(models: dict[str, Any], model: str, deployment: dict[str, Any]) -> str:
    naming = models["naming"]
    entries = [entry for entry in models.get("catalog", []) if entry.get("name") == model]
    if len(entries) > 1:
        raise ValueError("ambiguous catalog model identity")
    base = deployment_base(entries[0]) if entries else model
    return str(naming.get("pattern") or PATTERN).format(
        model=base,
        subscriptionToken=naming["subscriptionToken"],
        region=deployment["region"],
        skuShort=naming["skuShort"][deployment["sku"]],
    )
