#!/usr/bin/env python3
"""Generate APIM model and realtime routing from the authoritative catalog."""

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
REALTIME_OUTPUT_PATH = ROOT / "infra" / "policies" / "realtime-routing.xml"
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


def generate_realtime_policy(models: dict[str, Any]) -> str:
    naming = models["naming"]
    routes: list[str] = []
    for model in models["catalog"]:
        if model["category"] != "realtime":
            continue
        for deployment in model["deployments"]:
            name = deployment_name(
                model=model["name"],
                subscription_token=naming["subscriptionToken"],
                region=deployment["region"],
                sku=deployment["sku"],
                sku_short=naming["skuShort"],
            )
            routes.append(
                "      <when condition=\"@(&quot;"
                + html.escape(name, quote=True)
                + "&quot;.Equals(context.Request.Url.Query.GetValueOrDefault"
                "(&quot;deployment&quot;, &quot;&quot;), "
                "StringComparison.OrdinalIgnoreCase))\">\n"
                "        <set-backend-service base-url=\"{{foundry-"
                + deployment["region"]
                + "-endpoint}}/openai/realtime\" />\n"
                "      </when>"
            )

    if not routes:
        raise ValueError("infra/models.json contains no realtime deployments")

    return (
        "<policies>\n"
        "  <inbound>\n"
        "    <base />\n"
        "    <choose>\n"
        + "\n".join(routes)
        + "\n"
        "      <otherwise>\n"
        "        <return-response>\n"
        "          <set-status code=\"404\" reason=\"Realtime deployment is not in the AI4IA catalog\" />\n"
        "          <set-body>{\"error\":{\"code\":\"model_not_allowed\",\"message\":\"The requested realtime deployment is not allowed by the gateway catalog.\"}}</set-body>\n"
        "        </return-response>\n"
        "      </otherwise>\n"
        "    </choose>\n"
        "    <set-header name=\"x-correlation-id\" exists-action=\"override\">\n"
        "      <value>@(context.RequestId.ToString())</value>\n"
        "    </set-header>\n"
        "    <set-header name=\"Ocp-Apim-Subscription-Key\" exists-action=\"delete\" />\n"
        "    <set-header name=\"Authorization\" exists-action=\"delete\" />\n"
        "    <authentication-managed-identity resource=\"https://cognitiveservices.azure.com\" />\n"
        "  </inbound>\n"
        "  <backend><base /></backend>\n"
        "  <outbound><base /></outbound>\n"
        "  <on-error><base /></on-error>\n"
        "</policies>\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the checked-in fragment differs from generated output",
    )
    args = parser.parse_args()
    models = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    generated = generate()
    realtime_generated = generate_realtime_policy(models)

    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        realtime_current = (
            REALTIME_OUTPUT_PATH.read_text(encoding="utf-8")
            if REALTIME_OUTPUT_PATH.exists()
            else ""
        )
        if current != generated or realtime_current != realtime_generated:
            print(
                "Gateway policy catalog is stale. Run scripts/gen-gateway-policy.py.",
                file=sys.stderr,
            )
            return 1
        print("Gateway model and realtime policy catalogs are current.")
        return 0

    OUTPUT_PATH.write_text(generated, encoding="utf-8", newline="\n")
    REALTIME_OUTPUT_PATH.write_text(
        realtime_generated, encoding="utf-8", newline="\n"
    )
    print(f"Wrote {OUTPUT_PATH.relative_to(ROOT)}")
    print(f"Wrote {REALTIME_OUTPUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
