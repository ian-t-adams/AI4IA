#!/usr/bin/env python3
"""Validate infra/models.json internal consistency beyond JSON-schema shape.

Checks that the data-driven catalog cannot silently drop deployments:
  * every deployment.region is defined under `regions`
  * every deployment.sku has a short token under `naming.skuShort`
  * generated deployment names (model + region + skuShort) are unique
  * no duplicate (model, region) pairs

Exit non-zero on any violation. Safe to run locally or in CI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODELS = HERE.parent / "infra" / "models.json"


def main() -> int:
    data = json.loads(MODELS.read_text(encoding="utf-8"))
    regions = set(data["regions"].keys())
    sku_short = data["naming"]["skuShort"]
    errors: list[str] = []
    seen_names: dict[str, str] = {}
    seen_pairs: set[tuple[str, str]] = set()

    for model in data["catalog"]:
        name = model["name"]
        for dep in model["deployments"]:
            region = dep["region"]
            sku = dep["sku"]
            if region not in regions:
                errors.append(
                    f"{name}: deployment region '{region}' is not defined in regions "
                    f"({sorted(regions)})"
                )
                continue
            if sku not in sku_short:
                errors.append(
                    f"{name}: sku '{sku}' has no naming.skuShort token "
                    f"({sorted(sku_short)})"
                )
                continue
            pair = (name, region)
            if pair in seen_pairs:
                errors.append(f"{name}: duplicate deployment for region '{region}'")
            seen_pairs.add(pair)
            dep_name = f"{name}-slurmfactory-{region}-{sku_short[sku]}"
            if dep_name in seen_names:
                errors.append(
                    f"duplicate deployment name '{dep_name}' "
                    f"({seen_names[dep_name]} and {name})"
                )
            seen_names[dep_name] = name

    deployments = sum(len(m["deployments"]) for m in data["catalog"])
    if errors:
        print(f"FAIL: {len(errors)} catalog issue(s) in {MODELS}:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(
        f"OK: {len(data['catalog'])} models, {deployments} deployments across "
        f"{len(regions)} regions; all regions/SKUs/names consistent."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
