#!/usr/bin/env python3
"""Verify the target subscription actually offers every model in the catalog.

`infra/models.json` is the source of truth for what gets deployed, but it says
nothing about what a *given* subscription is entitled to. Model availability is
per subscription, per region: limited-access models (o3-pro and friends) require
an approved request, Marketplace/partner models depend on the offer being
enabled, and pinned versions are retired on Azure's schedule, not ours.

None of that is visible until `azd provision` is already running. Foundry model
deployments are created late, so a missing model fails after the resource group,
Foundry accounts, gateway, and data tier exist -- the expensive, slow part
succeeds and then the run dies. Checking first turns a 30-minute partial deploy
into a 30-second answer.

This is an operator preflight, not a CI gate: it needs `az login` and a selected
subscription. Run it before the first provision in a new tenant/subscription.
See docs/runbooks/deployment.md ("Moving to a new subscription or tenant").

Usage:
    az account set --subscription <id>
    python scripts/check-model-availability.py
    python scripts/check-model-availability.py --region eastus2   # narrow it
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
MODELS_FILE = ROOT / "infra" / "models.json"


def _az(*args: str) -> subprocess.CompletedProcess[str]:
    # Resolve the executable: on Windows the CLI is az.cmd, and CreateProcess
    # will not launch a batch file from a bare name.
    executable = shutil.which("az")
    if executable is None:
        raise SystemExit(
            "ERROR: the Azure CLI (az) is not on PATH. Install it, run `az login`, "
            "and select the target subscription with `az account set --subscription <id>`."
        )
    return subprocess.run(
        [executable, *args], capture_output=True, text=True, check=False, encoding="utf-8"
    )


def catalog_requirements(models: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Group the catalog's deployments by region.

    Returns {region: [{name, sku, version}, ...]}.
    """
    by_region: dict[str, list[dict[str, str]]] = {}
    for entry in models.get("catalog", []):
        for deployment in entry.get("deployments", []):
            region = deployment.get("region")
            if not region:
                continue
            by_region.setdefault(region, []).append(
                {
                    "name": entry["name"],
                    "sku": deployment.get("sku", ""),
                    "version": str(deployment.get("version", "")),
                }
            )
    return by_region


def offered_models(region: str) -> list[dict[str, Any]]:
    result = _az("cognitiveservices", "model", "list", "--location", region, "-o", "json")
    if result.returncode != 0:
        raise SystemExit(
            f"ERROR: could not list models in {region}. Run `az login` and "
            f"`az account set --subscription <id>` first.\n{result.stderr.strip()}"
        )
    return json.loads(result.stdout or "[]")


def index_offered(raw: Iterable[dict[str, Any]]) -> dict[str, dict[str, set[str]]]:
    """Index the API response as {model_name_casefolded: {sku: {versions}}}.

    Model names are compared casefolded because the catalog and the API disagree
    on case for partner models (``Cohere-rerank-v4.0-pro`` vs the API's own
    casing), and Azure treats deployment model names case-insensitively.
    """
    index: dict[str, dict[str, set[str]]] = {}
    for item in raw:
        model = item.get("model") or {}
        name = model.get("name")
        if not name:
            continue
        version = str(model.get("version", ""))
        skus = index.setdefault(name.casefold(), {})
        for sku in model.get("skus") or []:
            sku_name = sku.get("name")
            if sku_name:
                skus.setdefault(sku_name, set()).add(version)
    return index


def evaluate(
    required: list[dict[str, str]], index: dict[str, dict[str, set[str]]]
) -> tuple[list[str], list[str]]:
    """Compare one region's requirements against what it offers.

    Returns (errors, warnings). A missing model or SKU is an error -- the
    deployment cannot succeed. A model offered under a *different* version is a
    warning: Azure will often accept the deployment and roll the version
    forward, and treating it as fatal would block a standup over a routine
    version retirement.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for item in required:
        name, sku, version = item["name"], item["sku"], item["version"]
        skus = index.get(name.casefold())
        if skus is None:
            errors.append(
                f"{name}: not offered in this subscription/region. "
                "Limited-access models need an approved access request; partner "
                "models need the Marketplace offer enabled."
            )
            continue
        if sku not in skus:
            errors.append(
                f"{name}: offered, but not with SKU {sku} (available: {', '.join(sorted(skus))})."
            )
            continue
        versions = skus[sku]
        if version and versions and version not in versions:
            warnings.append(
                f"{name} ({sku}): catalog pins version {version}; "
                f"this subscription offers {', '.join(sorted(versions))}."
            )
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--region",
        action="append",
        help="only check this region (repeatable); defaults to every region in the catalog",
    )
    args = parser.parse_args()

    models = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    by_region = catalog_requirements(models)
    regions = args.region or sorted(by_region)

    total_errors = 0
    total_warnings = 0
    for region in regions:
        required = by_region.get(region, [])
        if not required:
            print(f"{region}: no deployments in the catalog; skipping.")
            continue
        print(f"Checking {len(required)} deployments in {region} ...", flush=True)
        index = index_offered(offered_models(region))
        errors, warnings = evaluate(required, index)
        # Findings go to stdout, not stderr. They are the report -- and when the
        # two streams are merged (a CI log, `2>&1`, a terminal) the OS does not
        # guarantee their relative order, so splitting them shuffles each
        # finding away from the region heading it belongs to. Only the final
        # verdict goes to stderr, where a caller grepping for failure looks.
        for warning in warnings:
            print(f"  WARNING: {warning}")
        for error in errors:
            print(f"  ERROR: {error}")
        if not errors and not warnings:
            print(f"  all {len(required)} deployments are available.")
        total_errors += len(errors)
        total_warnings += len(warnings)

    print(
        f"\n{total_errors} blocking problem(s), {total_warnings} warning(s) "
        f"across {len(regions)} region(s).",
        flush=True,
    )
    if total_errors:
        print(
            "\nProvisioning will fail on the blocking problems above. Either request "
            "access to the model in this subscription, or remove its deployment from "
            "infra/models.json and re-run `python scripts/gen-model-catalog.py`.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
