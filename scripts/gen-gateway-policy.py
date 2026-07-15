#!/usr/bin/env python3
"""Generate the APIM endpoint fragment from the authoritative model catalog."""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODELS_PATH = ROOT / "infra" / "models.json"
TEMPLATE_PATH = ROOT / "infra" / "policies" / "simplel7proxy-endpoints.template.xml"
OUTPUT_PATH = ROOT / "infra" / "policies" / "simplel7proxy-endpoints.xml"
CATALOG_MARKER = "__AI4IA_BACKEND_CATALOG__"
ATTEMPTS_MARKER = "__AI4IA_MAX_IMMEDIATE_ATTEMPTS__"


def deployment_name(
    *,
    model: str,
    subscription_token: str,
    region: str,
    sku: str,
    sku_short: dict[str, str],
) -> str:
    return f"{model}-{subscription_token}-{region}-{sku_short[sku]}"


def timeout_seconds(category: str) -> int:
    if category in {"image", "video"}:
        return 240
    return 120


def backend_row(
    *,
    label: str,
    region: str,
    deployment: str,
    priority: int,
    timeout: int,
) -> str:
    named_value = f"{{{{foundry-{region}-endpoint}}}}"
    return (
        f'                ["{label}"] = new JObject {{ '
        f'["url"] = "{named_value}", ["path"] = "openai", '
        f'["deployment"] = "{deployment}", ["priority"] = {priority}, '
        '["acceptablePriorities"] = "1, 2, 3", '
        f'["timeout"] = {timeout}, ["bufferResponse"] = false, ["auth"] = "MI" }}'
    )


def render_catalog(models: dict[str, Any]) -> tuple[str, int]:
    naming = models["naming"]
    subscription_token = naming["subscriptionToken"]
    sku_short = naming["skuShort"]
    blocks: list[str] = []
    max_attempts = 1

    for model in models["catalog"]:
        deployments = model["deployments"]
        max_attempts = max(max_attempts, len(deployments))
        timeout = timeout_seconds(model["category"])
        resolved = [
            {
                "region": deployment["region"],
                "name": deployment_name(
                    model=model["name"],
                    subscription_token=subscription_token,
                    region=deployment["region"],
                    sku=deployment["sku"],
                    sku_short=sku_short,
                ),
            }
            for deployment in deployments
        ]

        for requested in resolved:
            ordered = sorted(
                resolved,
                key=lambda candidate: candidate["region"] != requested["region"],
            )
            rows = [
                backend_row(
                    label=candidate["region"].upper(),
                    region=candidate["region"],
                    deployment=candidate["name"],
                    priority=1 if candidate["region"] == requested["region"] else 2,
                    timeout=timeout,
                )
                for candidate in ordered
            ]
            block = (
                f'            ["{requested["name"]}"] = new JObject {{\n'
                + ",\n".join(rows)
                + "\n            }"
            )
            blocks.append(block)

    if not blocks:
        raise ValueError("infra/models.json contains no model deployments")
    return ",\n\n".join(blocks), max_attempts


def generate() -> str:
    models = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count(CATALOG_MARKER) != 1 or template.count(ATTEMPTS_MARKER) != 3:
        raise ValueError("gateway policy template markers are missing or duplicated")
    catalog, max_attempts = render_catalog(models)
    return template.replace(CATALOG_MARKER, html.escape(catalog, quote=True)).replace(
        ATTEMPTS_MARKER, str(max_attempts)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in fragment differs from generated output",
    )
    args = parser.parse_args()
    generated = generate()

    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        if current != generated:
            print(
                "Gateway policy catalog is stale. Run scripts/gen-gateway-policy.py.",
                file=sys.stderr,
            )
            return 1
        print("Gateway policy catalog is current.")
        return 0

    OUTPUT_PATH.write_text(generated, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
