#!/usr/bin/env python3
"""Verify the target subscription offers -- and has quota for -- every catalog model.

`infra/models.json` is the source of truth for what gets deployed, but it says
nothing about what a *given* subscription is entitled to. Three independent
things can be wrong, and a subscription can pass some while failing others:

* **Availability** -- is the model offered here at all? This is per subscription,
  per region: limited-access models (o3-pro and friends) require an approved
  request, Marketplace/partner models depend on the offer being enabled, and
  pinned versions are retired on Azure's schedule, not ours.
* **Lifecycle** -- is the model still accepting *new* deployments? A deprecating
  model can remain serviceable in an exact existing deployment while an addition
  or model/version/SKU/capacity change fails with `ServiceModelDeprecating`. The
  preflight inventories the target resource group: exact Succeeded deployments
  warn and reconcile; absent or drifted desired deployments block.
  Dated retirement observations additionally retain SKU and inference dates,
  upgrade posture, and catalog/deployed drift. Under retirement-admission-v1,
  authoritative desired-target dates within seven days block additions/changes;
  exact existing deployments still warn. Unknown and public dates never become
  authoritative retirement deadlines.
* **Quota** -- is there capacity left to deploy it? A brand-new subscription is
  offered nearly everything but ships with small default quotas, and several
  image/realtime/audio models default to caps in the single digits. Availability
  says yes; the deployment still fails with `InsufficientQuota`. Note that only
  `capacity > limit` is treated as blocking -- see `evaluate_quota` for why the
  reported `currentValue` is not trustworthy enough to fail a run on.

None of that is visible until `azd provision` is already running. Foundry model
deployments are created late, so either failure lands after the resource group,
Foundry accounts, gateway, and data tier exist -- the expensive, slow part
succeeds and then the run dies. Checking first turns a 30-minute partial deploy
into a 30-second answer.

This is a provisioning preflight, not a credential-free PR gate: it needs an
Azure CLI login and the target subscription selected. azure.yaml runs it in the
preprovision lifecycle hook, before ARM creates shared or paid resources. Direct
operator invocation remains useful for diagnosis. See docs/runbooks/deployment.md
("Moving to a new subscription or tenant").

Usage:
    az account set --subscription <id>
    python scripts/check-model-availability.py
    python scripts/check-model-availability.py --region eastus2   # narrow it
    python scripts/check-model-availability.py --skip-quota       # availability only
    python scripts/check-model-availability.py --retirement-report <output-directory>

The report mode reads inventory and offerings only (not quota), emits bounded
JSON/Markdown and a generated documentation preview, and never changes Azure or
the catalog. It requires explicit subscription and target environment context.
Its exit codes are 0 = complete/clear, 1 = attention, 2 = incomplete/unknown.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import _model_retirement as retirement
import _capacity_evidence as capacity_evidence
from _production_capacity import PROFILES, bind_scope, check_live_pools, effective_capacity, parse_policy

ROOT = Path(__file__).resolve().parents[1]
MODELS_FILE = ROOT / "infra" / "models.json"
MAX_AZURE_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_OFFERED_MODELS = 8192
MAX_REPORT_REGIONS = 8


def _az(*args: str) -> subprocess.CompletedProcess[str]:
    # Resolve the executable: on Windows the CLI is az.cmd, and CreateProcess
    # will not launch a batch file from a bare name.
    executable = shutil.which("az")
    if executable is None:
        raise SystemExit(
            "ERROR: the Azure CLI (az) is not on PATH. Install it, run `az login`, "
            "and select the target subscription with `az account set --subscription <id>`."
        )
    try:
        return subprocess.run(
            [executable, *args], capture_output=True, text=True, check=False,
            encoding="utf-8", timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit("ERROR: Azure CLI read timed out after 30 seconds; evidence is unavailable.") from exc


def active_subscription(expected_subscription_id: str | None = None) -> dict[str, str]:
    """Require usable Azure CLI context and, when known, the azd target subscription."""
    result = _az(
        "account",
        "show",
        "--query",
        "{id:id,name:name,tenantId:tenantId}",
        "-o",
        "json",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "Azure CLI returned no account."
        raise SystemExit(
            "ERROR: the model availability/quota preflight requires Azure CLI "
            "credentials for the target subscription. Run `az login` and "
            "`az account set --subscription <id>` before `azd provision`.\n"
            f"Azure CLI: {detail}"
        )
    try:
        account = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "ERROR: `az account show` returned invalid JSON; model availability/quota "
            f"cannot be evaluated safely: {exc.msg}."
        ) from exc
    if not isinstance(account, dict):
        raise SystemExit("ERROR: Azure CLI account context was not an object; subscription is unknown.")
    subscription_id = str(account.get("id") or "").strip()
    if not subscription_id:
        raise SystemExit(
            "ERROR: Azure CLI returned no active subscription id; run `az account set "
            "--subscription <id>` before `azd provision`."
        )
    expected = (expected_subscription_id or "").strip()
    if expected and subscription_id.casefold() != expected.casefold():
        raise SystemExit(
            "ERROR: Azure CLI is authenticated to subscription "
            f"{subscription_id}, but azd will provision {expected}. Run `az account set "
            f"--subscription {expected}` so the availability/quota preflight checks "
            "the subscription that will be charged."
        )
    return {
        "id": subscription_id,
        "name": str(account.get("name") or "").strip(),
        "tenantId": str(account.get("tenantId") or "").strip(),
    }


def catalog_requirements(
    models: dict[str, Any],
    *,
    include_anthropic: bool = True,
    capacity_profile: str = "baseline",
) -> dict[str, list[dict[str, Any]]]:
    """Group desired deployment records by region, including their exact ARM names."""
    if capacity_profile not in PROFILES:
        raise capacity_evidence.EvidenceError("invalid_capacity_profile")
    if capacity_profile == "production":
        parse_policy(models, required=True, include_anthropic=include_anthropic)
    naming = models.get("naming") or {}
    pattern = str(
        naming.get("pattern")
        or "{model}-{subscriptionToken}-{region}-{skuShort}"
    )
    subscription_token = str(naming.get("subscriptionToken") or "")
    sku_short = naming.get("skuShort") or {}
    by_region: dict[str, list[dict[str, Any]]] = {}
    for entry in models.get("catalog", []):
        if not include_anthropic and entry.get("format") == "Anthropic":
            continue
        for deployment in entry.get("deployments", []):
            region = deployment.get("region")
            sku = deployment.get("sku", "")
            if not region:
                continue
            deployment_name = pattern.format(
                model=entry["name"],
                subscriptionToken=subscription_token,
                region=region,
                skuShort=sku_short[sku],
            )
            by_region.setdefault(region, []).append(
                {
                    "deploymentName": deployment_name,
                    "name": entry["name"],
                    "format": entry.get("format", "OpenAI"),
                    "sku": sku,
                    "version": str(deployment.get("version", "")),
                    "capacity": effective_capacity(deployment, capacity_profile),
                    "capacityPool": (
                        deployment.get("maxCapacityPool")
                        if capacity_profile == "maximum"
                        else None
                    ),
                    "versionUpgradeOption": "NoAutoUpgrade",
                    "region": region,
                }
            )
    return by_region


def _json_result(result: subprocess.CompletedProcess[str], context: str) -> Any:
    if result.returncode != 0:
        detail = result.stderr.strip() or "Azure CLI returned no detail."
        raise SystemExit(
            f"ERROR: {context}; existing-state lifecycle safety cannot be evaluated.\n"
            f"Azure CLI: {detail}"
        )
    if len((result.stdout or "").encode("utf-8")) > MAX_AZURE_RESPONSE_BYTES:
        raise SystemExit(f"ERROR: {context}; Azure CLI response exceeded the 16 MiB read budget.")
    try:
        return json.loads(result.stdout or "")
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"ERROR: {context}; Azure CLI returned invalid JSON: {exc.msg}."
        ) from exc


def _normal_location(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _foundry_accounts_from_output(raw: str | None) -> dict[str, str]:
    """Read prior azd outputs when available; malformed outputs must not be guessed around."""
    if not raw or not raw.strip():
        return {}
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "ERROR: AZURE_FOUNDRY_ENDPOINTS is invalid JSON; cannot identify the "
            f"accounts whose existing deployments would be reconciled: {exc.msg}."
        ) from exc
    found: dict[str, str] = {}
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise SystemExit("ERROR: AZURE_FOUNDRY_ENDPOINTS must be an array of account/region objects.")
    for entry in entries:
        region = _normal_location(entry.get("region"))
        account_name = str(entry.get("accountName") or "").strip()
        if not region or not account_name:
            raise SystemExit("ERROR: AZURE_FOUNDRY_ENDPOINTS has an incomplete account/region identity.")
        if region in found and found[region].casefold() != account_name.casefold():
            raise SystemExit(
                f"ERROR: AZURE_FOUNDRY_ENDPOINTS names multiple accounts for {region}; "
                "existing-state lifecycle safety cannot choose one."
            )
        found[region] = account_name
    return found


def existing_deployment_inventory(
    models: dict[str, Any],
    *,
    resource_group: str | None,
    environment_name: str | None,
    foundry_endpoints_raw: str | None = None,
    region_reads: dict[str, retirement.SourceRead] | None = None,
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
    """Inventory exact deployments azd would reconcile, or enter explicit addition mode.

    No context means a manual/greenfield check: every desired deployment is treated
    as an addition. In an azd hook the target resource group/environment are known;
    any Azure inventory error fails rather than incorrectly exempting a lifecycle
    block. A report caller may supply region_reads to retain successful regions
    alongside explicit unavailable states; preprovision never opts into that
    partial-read mode. All operations are read-only.
    """
    resource_group = (resource_group or "").strip()
    environment_name = (environment_name or "").strip()
    if not resource_group and environment_name:
        workload = (os.environ.get("AI4IA_WORKLOAD") or "ai4ia").strip()
        resource_group = f"rg-{workload}-{environment_name}"
    if not resource_group:
        return {}, [
            (
                "No target resource group/environment was supplied; lifecycle checking "
                "is in greenfield/addition mode, so deprecated or deprecating desired "
                "deployments remain blocking. Set AZURE_ENV_NAME (or --resource-group "
                "and --environment-name) to evaluate an existing routine reconcile."
            )
        ]

    exists_result = _az("group", "exists", "--name", resource_group, "-o", "json")
    if exists_result.returncode != 0:
        _json_result(exists_result, f"could not test whether resource group {resource_group} exists")
    exists_text = str(exists_result.stdout or "").strip().casefold()
    if exists_text not in {"true", "false"}:
        raise SystemExit(
            f"ERROR: `az group exists` returned {exists_text!r} for {resource_group}; "
            "refusing to guess existing lifecycle state."
        )
    if exists_text == "false":
        if region_reads is not None:
            for region in models.get("regions") or {}:
                region_reads[region] = retirement.SourceRead(
                    "deployment-inventory", region, "observed", retirement.timestamp(datetime.now(UTC)),
                    "target-resource-group-absent",
                )
        return {}, [
            (
                f"Target resource group {resource_group} does not exist; lifecycle checking "
                "is in greenfield/addition mode."
            )
        ]

    accounts = _json_result(
        _az("cognitiveservices", "account", "list", "--resource-group", resource_group, "-o", "json"),
        f"could not list Cognitive Services accounts in {resource_group}",
    )
    if (
        not isinstance(accounts, list) or len(accounts) > 256
        or any(not isinstance(account, dict) for account in accounts)
    ):
        raise SystemExit(
            f"ERROR: Cognitive Services account inventory for {resource_group} was not a bounded object array."
        )
    explicit_accounts = _foundry_accounts_from_output(foundry_endpoints_raw)
    foundry_token = str((models.get("naming") or {}).get("foundryToken") or "")
    inventory: dict[tuple[str, str], dict[str, Any]] = {}

    for region in (models.get("regions") or {}):
        try:
            regional = _regional_deployment_inventory(
                accounts, region=region, resource_group=resource_group,
                expected_name=explicit_accounts.get(_normal_location(region)),
                environment_name=environment_name, foundry_token=foundry_token,
            )
        except (SystemExit, OSError):
            if region_reads is None:
                raise
            region_reads[region] = retirement.SourceRead(
                "deployment-inventory", region, "unavailable", retirement.timestamp(datetime.now(UTC)),
                "inventory-read-failed-or-ambiguous",
            )
            continue
        inventory.update(regional)
        if region_reads is not None:
            region_reads[region] = retirement.SourceRead(
                "deployment-inventory", region, "observed", retirement.timestamp(datetime.now(UTC))
            )
    return inventory, []


def _regional_deployment_inventory(
    accounts: list[dict[str, Any]],
    *,
    region: str,
    resource_group: str,
    expected_name: str | None,
    environment_name: str,
    foundry_token: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Read a whole account atomically so malformed rows cannot become partial absence."""
    normalized_region = _normal_location(region)
    if expected_name:
        candidates = [
            account for account in accounts
            if str(account.get("name") or "").casefold() == expected_name.casefold()
        ]
    elif environment_name:
        prefix = f"mf-{foundry_token}-{environment_name}-{region}-".casefold()
        candidates = [
            account for account in accounts
            if str(account.get("kind") or "").casefold() == "aiservices"
            and _normal_location(account.get("location")) == normalized_region
            and str(account.get("name") or "").casefold().startswith(prefix)
        ]
    else:
        raise SystemExit(
            "ERROR: the target resource group exists, but neither AZURE_ENV_NAME "
            "nor AZURE_FOUNDRY_ENDPOINTS identifies the Foundry accounts. Refusing "
            "to guess whether deprecated deployments would be changed."
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(str(account.get("name")) for account in candidates))
        raise SystemExit(
            f"ERROR: multiple candidate Foundry accounts for {region} in "
            f"{resource_group}: {names}. Refusing an ambiguous lifecycle exemption."
        )
    if not candidates:
        return {}
    if (
        str(candidates[0].get("kind") or "").casefold() != "aiservices"
        or _normal_location(candidates[0].get("location")) != normalized_region
    ):
        raise SystemExit("ERROR: the selected Foundry account has a different kind or region.")
    account_name = str(candidates[0].get("name") or "")
    deployments = _json_result(
        _az(
            "cognitiveservices", "account", "deployment", "list",
            "--resource-group", resource_group, "--name", account_name, "-o", "json",
        ),
        f"could not list model deployments for {account_name}",
    )
    if (
        not isinstance(deployments, list) or len(deployments) > 512
        or any(not isinstance(deployment, dict) for deployment in deployments)
    ):
        raise SystemExit(
            f"ERROR: deployment inventory for {account_name} was not a bounded object array."
        )
    inventory = {}
    for deployment in deployments:
        deployment_name = str(deployment.get("name") or "").strip()
        if not deployment_name:
            raise SystemExit("ERROR: deployment inventory contains a record without a name.")
        properties = deployment.get("properties") or {}
        if not isinstance(properties, dict):
            raise SystemExit("ERROR: deployment inventory properties were not an object.")
        model = properties.get("model") or {}
        sku = deployment.get("sku") or {}
        if not isinstance(model, dict) or not isinstance(sku, dict):
            raise SystemExit("ERROR: deployment inventory model/SKU were not objects.")
        if (normalized_region, deployment_name.casefold()) in inventory:
            raise SystemExit("ERROR: deployment inventory contains duplicate deployment identities.")
        inventory[(normalized_region, deployment_name.casefold())] = {
            "accountName": account_name,
            "deploymentName": deployment_name,
            "region": region,
            "modelName": str(model.get("name") or ""),
            "format": str(model.get("format") or ""),
            "version": str(model.get("version") or ""),
            "sku": str(sku.get("name") or ""),
            "capacity": sku.get("capacity"),
            "versionUpgradeOption": str(properties.get("versionUpgradeOption") or ""),
            "provisioningState": str(properties.get("provisioningState") or ""),
        }
    return inventory


def existing_deployment_drift(
    required: dict[str, Any],
    inventory: dict[tuple[str, str], dict[str, Any]] | None,
) -> list[str]:
    """Return differences that make the desired resource an addition/update."""
    deployment_name = str(required.get("deploymentName") or "").strip()
    region = _normal_location(required.get("region"))
    existing = (inventory or {}).get((region, deployment_name.casefold()))
    if not deployment_name or existing is None:
        return ["deployment is absent"]

    differences: list[str] = []
    comparisons = (
        ("model", required.get("name"), existing.get("modelName"), True),
        ("format", required.get("format"), existing.get("format"), True),
        ("version", str(required.get("version") or ""), existing.get("version"), False),
        ("SKU", required.get("sku"), existing.get("sku"), True),
        (
            "versionUpgradeOption",
            required.get("versionUpgradeOption"),
            existing.get("versionUpgradeOption"),
            True,
        ),
    )
    for label, desired, actual, case_insensitive in comparisons:
        desired_text = str(desired or "")
        actual_text = str(actual or "")
        equal = (
            desired_text.casefold() == actual_text.casefold()
            if case_insensitive
            else desired_text == actual_text
        )
        if not equal:
            differences.append(f"{label} is {actual_text or '<missing>'}, wants {desired_text}")
    actual_capacity = existing.get("capacity")
    if type(actual_capacity) is not int or actual_capacity < 0:
        actual_capacity = -1
    desired_capacity = int(required.get("capacity") or 0)
    if actual_capacity != desired_capacity:
        differences.append(f"capacity is {actual_capacity}, wants {desired_capacity}")
    if str(existing.get("provisioningState") or "").casefold() != "succeeded":
        differences.append(
            "provisioningState is "
            f"{existing.get('provisioningState') or '<missing>'}, wants Succeeded"
        )
    return differences


def all_deployments_exact_existing(
    required: Iterable[dict[str, Any]],
    inventory: dict[tuple[str, str], dict[str, Any]] | None,
) -> bool:
    """True only when every desired record is an exact Succeeded deployment."""
    items = list(required)
    return bool(items) and all(
        not existing_deployment_drift(item, inventory) for item in items
    )


def offered_models(region: str) -> list[dict[str, Any]]:
    offered = _json_result(
        _az("cognitiveservices", "model", "list", "--location", region, "-o", "json"),
        f"could not list model offerings in {region}",
    )
    if not isinstance(offered, list) or len(offered) > MAX_OFFERED_MODELS:
        raise SystemExit("ERROR: model offerings were not a bounded object array; coverage is unknown.")
    for row in offered:
        if not isinstance(row, dict) or not isinstance(row.get("model"), dict):
            raise SystemExit("ERROR: model offering shape is invalid; coverage is unknown.")
        model = row["model"]
        if any(not isinstance(model.get(key), str) or not model[key] for key in ("name", "format", "version")):
            raise SystemExit("ERROR: model offering identity is incomplete; coverage is unknown.")
        skus = model.get("skus") or []
        if not isinstance(skus, list) or any(not isinstance(sku, dict) for sku in skus):
            raise SystemExit("ERROR: model offering SKU shape is invalid; coverage is unknown.")
        if model.get("deprecation") is not None and not isinstance(model["deprecation"], dict):
            raise SystemExit("ERROR: model deprecation shape is invalid; coverage is unknown.")
    return offered


def catalog_offerings(
    required: list[dict[str, Any]], offered: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Availability/lifecycle must not borrow a same-named model from another format."""
    identities = {
        (item["name"].casefold(), item["format"].casefold()) for item in required
    }
    return [
        row for row in offered
        if (
            str((row.get("model") or {}).get("name") or "").casefold(),
            str((row.get("model") or {}).get("format") or "").casefold(),
        ) in identities
    ]


def retirement_observations(
    required: list[dict[str, Any]],
    offered: list[dict[str, Any]] | None,
    inventory: dict[tuple[str, str], dict[str, Any]],
    *,
    region: str,
    now: datetime,
    inventory_state: retirement.InventoryState,
    inventory_observed_at: datetime | None,
    public: tuple[retirement.PublicObservation, ...] = (),
) -> list[retirement.RetirementObservation]:
    observations = []
    seen = set()
    for item in required:
        key = (_normal_location(region), str(item["deploymentName"]).casefold())
        seen.add(key)
        observations.append(retirement.observe_deployment(
            item, inventory.get(key), existing_deployment_drift(item, inventory),
            offered, now=now, inventory_state=inventory_state,
            inventory_observed_at=inventory_observed_at, public=public,
        ))
    for key, deployed in sorted(inventory.items()):
        if key[0] == _normal_location(region) and key not in seen:
            observations.append(retirement.observe_deployment(
                None, deployed, ["deployment is outside the catalog"], offered,
                now=now, inventory_state=inventory_state,
                inventory_observed_at=inventory_observed_at, public=public,
            ))
    return observations


def run_retirement_report(
    args: argparse.Namespace, models: dict[str, Any], catalog_bytes: bytes
) -> int:
    """A separate read-only path: no provisioning preflight, provider registration or quota."""
    if args.retirement_report.resolve().is_relative_to(ROOT):
        raise ValueError("Retirement report output must be outside the source checkout.")
    started = datetime.now(UTC)
    claude_enabled = (os.environ.get("AI4IA_CLAUDE_ENABLED") or "").strip().casefold() in {
        "1", "true", "yes", "on"
    }
    by_region = catalog_requirements(
        models, include_anthropic=claude_enabled, capacity_profile=args.capacity_profile
    )
    regions = sorted(set(args.region or by_region))
    if not regions or len(regions) > MAX_REPORT_REGIONS or set(regions) - set(models["regions"]):
        raise ValueError("Retirement reporting requires one to eight catalog regions.")
    sources: list[retirement.SourceRead] = []
    public: tuple[retirement.PublicObservation, ...] = ()
    if args.public_evidence:
        try:
            public = retirement.load_public_observations(args.public_evidence, started)
            sources.append(retirement.SourceRead("public-evidence-file", None, "observed", retirement.timestamp(started)))
        except (OSError, ValueError):
            sources.append(retirement.SourceRead(
                "public-evidence-file", None, "unavailable", retirement.timestamp(started),
                "invalid-or-unreadable-public-evidence",
            ))
    authenticated = False
    inventory: dict[tuple[str, str], dict[str, Any]] = {}
    inventory_reads: dict[str, retirement.SourceRead] = {}
    environment = (args.environment_name or os.environ.get("AZURE_ENV_NAME") or "").strip()
    resource_group = (args.resource_group or os.environ.get("AZURE_RESOURCE_GROUP") or "").strip()
    endpoints = os.environ.get("AZURE_FOUNDRY_ENDPOINTS")
    try:
        expected = (os.environ.get("AZURE_SUBSCRIPTION_ID") or "").strip()
        if not expected or not (resource_group or environment) or not (environment or endpoints):
            raise SystemExit("Explicit report subscription and target account context are required.")
        active_subscription(expected)
        authenticated = True
        sources.append(retirement.SourceRead(
            "subscription-context", None, "observed", retirement.timestamp(datetime.now(UTC))
        ))
    except (SystemExit, OSError):
        sources.append(retirement.SourceRead(
            "subscription-context", None, "unavailable", retirement.timestamp(datetime.now(UTC)),
            "missing-context-or-subscription-read-failed",
        ))
    if authenticated:
        try:
            inventory, _ = existing_deployment_inventory(
                {**models, "regions": {region: models["regions"][region] for region in regions}},
                resource_group=resource_group, environment_name=environment,
                foundry_endpoints_raw=endpoints, region_reads=inventory_reads,
            )
        except (SystemExit, OSError):
            for region in regions:
                inventory_reads[region] = retirement.SourceRead(
                    "deployment-inventory", region, "unavailable", retirement.timestamp(datetime.now(UTC)),
                    "inventory-read-failed-or-ambiguous",
                )
    else:
        for region in regions:
            inventory_reads[region] = retirement.SourceRead(
                "deployment-inventory", region, "unavailable", retirement.timestamp(datetime.now(UTC)),
                "subscription-context-unavailable",
            )
    sources.extend(inventory_reads.values())
    observations = []
    for region in regions:
        offered = None
        if authenticated:
            try:
                offered = offered_models(region)
                sources.append(retirement.SourceRead(
                    "model-offerings", region, "observed", retirement.timestamp(datetime.now(UTC))
                ))
            except (SystemExit, OSError):
                sources.append(retirement.SourceRead(
                    "model-offerings", region, "unavailable", retirement.timestamp(datetime.now(UTC)),
                    "offering-read-failed-or-invalid",
                ))
        else:
            sources.append(retirement.SourceRead(
                "model-offerings", region, "unavailable", retirement.timestamp(datetime.now(UTC)),
                "subscription-context-unavailable",
            ))
        inventory_read = inventory_reads[region]
        inventory_at = (
            datetime.fromisoformat(inventory_read.observed_at)
            if inventory_read.status == "observed" else None
        )
        observations.extend(retirement_observations(
            by_region.get(region, []), offered, inventory, region=region, now=datetime.now(UTC),
            inventory_state=inventory_read.status, inventory_observed_at=inventory_at, public=public,
        ))
    report = retirement.build_report(
        observations, sources, now=datetime.now(UTC), catalog_bytes=catalog_bytes,
        capacity_profile=args.capacity_profile, include_anthropic=claude_enabled,
        public_count=len(public),
    )
    result = retirement.write_report(
        args.retirement_report, report,
        (ROOT / "docs" / "region-capability-matrix.md").read_text(encoding="utf-8"),
    )
    print(
        f"Retirement report: {report['status']}; {report['total_observations']} observations, "
        f"{report['unknown_observations']} unknown, {report['omitted_observations']} omitted. "
        "No catalog or Azure state was changed."
    )
    return result


def quota_usage(region: str) -> list[dict[str, Any]]:
    result = _az("cognitiveservices", "usage", "list", "--location", region, "-o", "json")
    if result.returncode != 0:
        raise SystemExit(
            f"ERROR: could not read quota in {region}. Run `az login` and "
            f"`az account set --subscription <id>` first.\n{result.stderr.strip()}"
        )
    return json.loads(result.stdout or "[]")


def _quota_keys(sku: str, model: str) -> list[str]:
    """Candidate lookup keys for one model+SKU, loosest last.

    Quota counters are *not* named after the model. They carry a publisher
    prefix the catalog never mentions (`OpenAI.` for first-party,
    `AIServices.` for partner models), and the model segment is spelled
    differently again: `model-router` counts against `ModelRouter`,
    `gpt-4.1-mini` against `gpt4.1-mini`, `o3-deep-research` against
    `o3-DeepResearch`. Stripping the prefix and every non-alphanumeric
    character reconciles all of those.

    Two conventions survive that:

    * partner counters drop a ``.0`` version suffix, so
      `Cohere-rerank-v4.0-pro` counts against `Cohere-Rerank-V4-Pro`; and
    * Azure-hosted partner variants append ``.Azure`` to the counter model
      (`claude-opus-4-8.Azure`) while the deployment model remains
      `claude-opus-4-8`.

    Both get normalized candidates rather than model-specific special cases.
    """

    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.casefold())

    sku_key = norm(sku)
    keys = [f"{sku_key}|{norm(model)}"]
    without_dot_zero = model.replace(".0", "")
    if without_dot_zero != model:
        keys.append(f"{sku_key}|{norm(without_dot_zero)}")
    without_azure_host = re.sub(r"\.azure$", "", model, flags=re.IGNORECASE)
    if without_azure_host != model:
        keys.append(f"{sku_key}|{norm(without_azure_host)}")
    return keys


def index_quota(raw: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index usage counters as {"<sku>|<model>": {limit, current, counter}}.

    Keys are normalised the same way as :func:`_quota_keys`, and the publisher
    prefix (`OpenAI.` / `AIServices.`) is dropped so both namespaces land in one
    index -- the catalog does not record which publisher a model belongs to.
    """
    index: dict[str, dict[str, Any]] = {}
    for item in raw:
        counter = (item.get("name") or {}).get("value")
        if not counter or counter.count(".") < 2:
            continue
        # "<publisher>.<sku>.<model>" -- the model segment may itself contain
        # dots (gpt4.1-mini), so split off publisher and SKU only.
        _publisher, _, remainder = counter.partition(".")
        sku, _, model = remainder.partition(".")
        if not sku or not model:
            continue
        for key in _quota_keys(sku, model):
            index.setdefault(
                key,
                {
                    "limit": float(item.get("limit") or 0),
                    "current": float(item.get("currentValue") or 0),
                    "counter": counter,
                },
            )
    return index


def evaluate_quota(
    required: list[dict[str, Any]],
    index: dict[str, dict[str, Any]],
    existing_deployments: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Compare one region's requested capacity against quota.

    Requested capacity is summed per model+SKU: several catalog deployments of
    the same model in one region draw down a single shared counter, so checking
    them individually would let a pair that each fit -- but together do not --
    pass.

    **Only `capacity > limit` can be an error here.** It blocks when any desired
    deployment in that model+SKU group is absent or drifted, because reconcile
    would need Azure to accept the total desired capacity. If every desired
    deployment is already Succeeded and exact, a reduced limit is only a warning:
    routine reconcile does not request capacity, although later recreation would
    fail without a quota increase.

    Exceeding *remaining* quota (`limit - currentValue`) is only a **warning**,
    because a per-region reading of `currentValue` is not what ARM enforces
    against in this region. The counter is **subscription-wide**, and the same
    aggregate is replicated verbatim into every region's response -- see
    :func:`evaluate_shared_quota`, which is the check that actually reasons about
    it. Locally that produces readings which look alarming and are not:

    * `AIServices.GlobalStandard.MAI-Image-2.5` reports `2/2` in **eastus2**, a
      region that does not offer the model at all. The only deployment is in
      westus.
    * `OpenAI.GlobalStandard.gpt-image-1.5` reports `9/9` in both eastus2 and
      swedencentral while *each* of those regions holds its own 9-capacity
      deployment -- 18 units against a counter that maxes out at 9, because the
      displayed value is clamped to the limit.
    * During a provision the value also carries in-flight reservations, so it is
      high precisely when a retry is about to succeed.

    Blocking per-region on it would strand a standup on a model that deploys --
    the exact failure mode this script exists to prevent.

    A counter that cannot be found is also a warning, because its absence is
    ambiguous: it may mean no quota is granted, or merely that Azure spells that
    counter in a way this mapping does not reconcile.
    """
    errors: list[str] = []
    warnings: list[str] = []

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in required:
        key = (item["sku"], item["name"])
        grouped.setdefault(key, []).append(item)

    for (sku, name), items in sorted(grouped.items()):
        capacity = sum(int(item.get("capacity") or 0) for item in items)
        exact_existing = all_deployments_exact_existing(items, existing_deployments)
        entry = next((index[k] for k in _quota_keys(sku, name) if k in index), None)
        if entry is None:
            warnings.append(
                f"{name} ({sku}): no quota counter matched; capacity {capacity} unverified. "
                "Confirm with `az cognitiveservices usage list -l <region>`."
            )
            continue
        limit = entry["limit"]
        if capacity > limit:
            detail = (
                f"{name} ({sku}): needs {capacity} but the subscription limit is "
                f"{limit:.0f} [{entry['counter']}]."
            )
            if exact_existing:
                warnings.append(
                    detail
                    + " Every desired deployment is already Succeeded and exactly "
                    "matches the catalog, so routine reconcile adds no capacity; "
                    "request quota before changing or recreating it."
                )
            else:
                errors.append(
                    detail
                    + " Request a quota increase or lower `capacity` in infra/models.json."
                )
            continue
        available = limit - entry["current"]
        if capacity > available:
            warnings.append(
                f"{name} ({sku}): needs {capacity}; limit {limit:.0f} is enough, but the "
                f"counter reports {entry['current']:.0f} already used, leaving "
                f"{available:.0f} [{entry['counter']}]. currentValue is unreliable "
                "(it saturates for undeployed models and includes in-flight "
                "reservations), so this is not treated as blocking -- but if the "
                "provision fails with InsufficientQuota on this model, it is real."
            )
        if capacity == limit:
            warnings.append(
                f"{name} ({sku}): needs {capacity}, which is the entire {limit:.0f} "
                f"limit [{entry['counter']}]. Zero headroom, so any concurrent "
                "reservation -- including a retry of this same provision -- fails it. "
                "Re-running usually clears it."
            )
    return errors, warnings


def evaluate_shared_quota(
    by_region: dict[str, list[dict[str, Any]]],
    index: dict[str, dict[str, Any]],
    existing_deployments: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Catch a model whose capacity fits each region but not the subscription.

    Model quota is **subscription-wide**, not per-region, and the per-region
    usage API replicates the same aggregate into every region's response. Proof,
    measured live: `AIServices.GlobalStandard.MAI-Image-2.5` reads `used=2 /
    limit=2` in **eastus2**, a region that does not offer MAI-Image at all; the
    subscription's only deployment of it sits in westus.

    That is how a catalog can pass every per-region check and still fail. Asking
    for capacity 2 in each of two regions is fine region-by-region -- 2 <= 2 both
    times -- but it is 4 against a shared limit of 2. Whichever region ARM
    reaches first wins, and the other dies with `InsufficientQuota`. It is
    deterministic, so re-running does not help; it just changes which region
    loses.

    Enforcement is not uniform across publishers, and the split is treated as
    observed rather than assumed:

    * `AIServices.*` (Microsoft-published) **is** enforced subscription-wide.
      MAI-Image-2.5/-Flash/-Pro each deployed in westus and then failed in
      swedencentral on exactly this. Reported as an **error**.
    * `OpenAI.*` is **not**: `gpt-image-1.5` holds a 9-capacity deployment in
      eastus2 *and* another in swedencentral -- 18 units against a limit of 9 --
      and both succeeded. Reported as a **warning**, because blocking it would
      reject a shape that demonstrably works.

    Single-region models are skipped: :func:`evaluate_quota` already covers them,
    and re-reporting would double-count. An over-limit group made entirely of
    exact Succeeded deployments is also warning-only because routine reconcile
    adds no shared capacity; any absent or drifted member retains the normal
    publisher-specific enforcement below.
    """
    errors: list[str] = []
    warnings: list[str] = []

    totals: dict[tuple[str, str], int] = {}
    regions: dict[tuple[str, str], set[str]] = {}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for region, required in by_region.items():
        for item in required:
            key = (item["sku"], item["name"])
            totals[key] = totals.get(key, 0) + int(item.get("capacity") or 0)
            regions.setdefault(key, set()).add(region)
            grouped.setdefault(key, []).append(item)

    for (sku, name), total in sorted(totals.items()):
        spread = regions[(sku, name)]
        if len(spread) < 2:
            continue
        entry = next((index[k] for k in _quota_keys(sku, name) if k in index), None)
        if entry is None or total <= entry["limit"]:
            continue
        counter = entry["counter"]
        where = ", ".join(sorted(spread))
        detail = (
            f"{name} ({sku}): {total} total across {len(spread)} regions ({where}) "
            f"exceeds the {entry['limit']:.0f} subscription-wide limit [{counter}]. "
            "Quota is shared across regions even though the usage API reports it "
            "per region."
        )
        if all_deployments_exact_existing(grouped[(sku, name)], existing_deployments):
            warnings.append(
                detail
                + " Every desired deployment is already Succeeded and exactly "
                "matches the catalog, so routine reconcile adds no shared capacity; "
                "request quota before changing or recreating one."
            )
        elif counter.partition(".")[0].casefold() == "openai":
            warnings.append(
                detail + " OpenAI-published models have been observed to enforce this "
                "per region (gpt-image-1.5 holds a full-limit deployment in two "
                "regions at once), so this is reported rather than blocking."
            )
        else:
            errors.append(
                detail + " Non-OpenAI models are enforced subscription-wide -- the "
                "first region to deploy consumes the quota and the rest fail with "
                "InsufficientQuota. Drop a region or request an increase."
            )
    return errors, warnings


def evaluate_declared_capacity_pools(
    by_region: dict[str, list[dict[str, Any]]],
    index: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Validate generated maximum capacities against their recorded Azure pool."""
    errors: list[str] = []
    warnings: list[str] = []
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    all_model_sku: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for required in by_region.values():
        for item in required:
            all_model_sku.setdefault((item["sku"], item["name"]), []).append(item)
            pool = str(item.get("capacityPool") or "")
            if not pool:
                warnings.append(
                    f"{item['name']} ({item['sku']}, {item['region']}): no "
                    "maxCapacity was recorded, so maximum falls back to baseline."
                )
                continue
            grouped.setdefault((item["sku"], item["name"], pool), []).append(item)

    for (sku, name, pool), items in sorted(grouped.items()):
        entry = next((index[key] for key in _quota_keys(sku, name) if key in index), None)
        if entry is None:
            warnings.append(
                f"{name} ({sku}, {pool}): no quota counter matched; maximum "
                "capacity cannot be revalidated."
            )
            continue
        total = sum(int(item.get("capacity") or 0) for item in items)
        if total > entry["limit"]:
            errors.append(
                f"{name} ({sku}, {pool}): maximum profile requests {total}, above "
                f"the {entry['limit']:.0f} limit [{entry['counter']}]. Regenerate "
                "the profile after quota changes."
            )
    for (sku, name), items in sorted(all_model_sku.items()):
        entry = next((index[key] for key in _quota_keys(sku, name) if key in index), None)
        if (
            entry is None
            or sku != "GlobalStandard"
            or entry["counter"].partition(".")[0].casefold() != "aiservices"
        ):
            continue
        total = sum(int(item.get("capacity") or 0) for item in items)
        if total > entry["limit"]:
            errors.append(
                f"{name} ({sku}): {total} total across all profile deployments exceeds "
                f"the {entry['limit']:.0f} subscription-global partner limit "
                f"[{entry['counter']}]. Regenerate the maximum profile."
            )
    return errors, warnings


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


def index_lifecycle(raw: Iterable[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Index lifecycle status as {model_name_casefolded: {version: status}}.

    Lifecycle is a property of the model *version*, not of the SKU, so it is
    indexed separately from :func:`index_offered` rather than folded into it.
    """
    index: dict[str, dict[str, str]] = {}
    for item in raw:
        model = item.get("model") or {}
        name = model.get("name")
        if not name:
            continue
        status = model.get("lifecycleStatus")
        if not status:
            continue
        versions = index.setdefault(name.casefold(), {})
        version = str(model.get("version", ""))
        prior = versions.get(version, "").casefold()
        if prior not in UNDEPLOYABLE_LIFECYCLE:
            versions[version] = str(status)
    return index


# Lifecycle states that cannot be assumed safe for a deployment addition or change.
UNDEPLOYABLE_LIFECYCLE = frozenset({"deprecating", "deprecated"})
DEPLOYABLE_LIFECYCLE = frozenset({"generallyavailable", "preview"})


def evaluate(
    required: list[dict[str, Any]],
    index: dict[str, dict[str, set[str]]],
    lifecycle: dict[str, dict[str, str]],
    existing_deployments: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Compare one region's requirements against what it offers.

    Returns (errors, warnings). A missing model or SKU is an error when the
    desired deployment is absent or drifted. A deprecating/deprecated desired
    version is blocking under the same condition. An exact Succeeded deployment
    is allowed for a routine reconcile with a migration warning because no model
    deployment addition/change is intended, even if Azure no longer lists its
    offer or SKU.

    A model offered under a *different* version is a warning: Azure will often
    accept the deployment and roll the version forward, and treating it as fatal
    would block a standup over a routine version retirement.

    ``lifecycle`` is a required argument rather than an optional one so a caller
    cannot silently skip the check -- which is exactly how the first cutover
    shipped a preflight that reported "78/78 available" while two of those 78
    could not be deployed.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for item in required:
        name, sku, version = item["name"], item["sku"], item["version"]
        drift = existing_deployment_drift(item, existing_deployments)
        deployment_name = str(item.get("deploymentName") or name)
        skus = index.get(name.casefold())
        if skus is None:
            if not drift:
                warnings.append(
                    f"{name}: no longer listed as offered, but exact existing "
                    f"deployment {deployment_name} is Succeeded and routine reconcile "
                    "does not create or change it; migrate before recreation is needed."
                )
            else:
                errors.append(
                    f"{name}: not offered in this subscription/region. "
                    "Limited-access models need an approved access request; partner "
                    "models need the Marketplace offer enabled."
                )
            continue
        if sku not in skus:
            if not drift:
                warnings.append(
                    f"{name}: SKU {sku} is no longer listed, but exact existing "
                    f"deployment {deployment_name} is Succeeded and routine reconcile "
                    "does not create or change it; migrate before recreation is needed."
                )
            else:
                errors.append(
                    f"{name}: offered, but not with SKU {sku} "
                    f"(available: {', '.join(sorted(skus))})."
                )
            continue

        status = (lifecycle.get(name.casefold()) or {}).get(version)
        if status:
            folded = status.casefold()
            if folded in UNDEPLOYABLE_LIFECYCLE:
                if not drift:
                    existing = (existing_deployments or {})[
                        (_normal_location(item.get("region")), deployment_name.casefold())
                    ]
                    warnings.append(
                        f"{name} ({version}): lifecycle is {status}, but exact existing "
                        f"deployment {deployment_name} in {item.get('region')} on "
                        f"{existing.get('accountName')} is Succeeded and matches the "
                        "desired model/version/SKU/capacity/version-upgrade posture. Routine "
                        "reconcile is allowed "
                        "because it does not add or change this deployment; migrate before "
                        "retirement."
                    )
                    continue
                alternatives = sorted(
                    v
                    for v, s in (lifecycle.get(name.casefold()) or {}).items()
                    if s.casefold() in DEPLOYABLE_LIFECYCLE
                )
                remedy = (
                    f"repin to version {', '.join(alternatives)}"
                    if alternatives
                    else "no deployable version is offered -- remove the model or "
                    "replace it with a successor in the same category"
                )
                errors.append(
                    f"{name} ({version}): lifecycle is {status}, and desired deployment "
                    f"{deployment_name} would be created or changed ({'; '.join(drift)}). "
                    "Azure can reject that operation with ServiceModelDeprecating even "
                    f"while an exact existing deployment keeps serving. {remedy}."
                )
                continue
            if folded not in DEPLOYABLE_LIFECYCLE:
                warnings.append(
                    f"{name} ({version}): unrecognized lifecycle status {status!r}; "
                    "lifecycle safety is unknown; no authoritative lifecycle block was inferred."
                )

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
    parser.add_argument(
        "--skip-quota",
        action="store_true",
        help="check availability only, skipping the quota comparison",
    )
    parser.add_argument(
        "--resource-group",
        help="target resource group for existing-deployment lifecycle checks",
    )
    parser.add_argument(
        "--environment-name",
        help="azd environment name used to identify this stack's Foundry accounts",
    )
    parser.add_argument(
        "--capacity-profile",
        choices=PROFILES,
        default=(
            os.environ.get("AI4IA_MODEL_CAPACITY_PROFILE") or "baseline"
        ).strip().casefold(),
        help="capacity profile to validate; defaults from AI4IA_MODEL_CAPACITY_PROFILE",
    )
    parser.add_argument(
        "--retirement-report",
        type=Path,
        help="read-only retirement report directory (JSON, Markdown, docs preview); skips quota",
    )
    parser.add_argument(
        "--public-evidence",
        type=Path,
        help="optional bounded JSON array of explicitly scoped Microsoft Learn date observations",
    )
    args = parser.parse_args()

    catalog_bytes = MODELS_FILE.read_bytes()
    models = json.loads(catalog_bytes)
    if args.capacity_profile not in PROFILES:
        parser.error("capacity profile must be baseline, production or maximum")
    if args.region and set(args.region) - set(models["regions"]):
        parser.error("--region must name a region in infra/models.json")
    claude_enabled = (
        os.environ.get("AI4IA_CLAUDE_ENABLED") or ""
    ).strip().casefold() in {"1", "true", "yes", "on"}
    environment_name = (args.environment_name or os.environ.get("AZURE_ENV_NAME") or "").strip()
    resource_group = (args.resource_group or os.environ.get("AZURE_RESOURCE_GROUP") or "").strip()
    if not resource_group and environment_name:
        resource_group = f"rg-{(os.environ.get('AI4IA_WORKLOAD') or 'ai4ia').strip()}-{environment_name}"
    try:
        policy = parse_policy(
            models, required=args.capacity_profile == "production", include_anthropic=claude_enabled,
        )
        if args.capacity_profile == "production":
            if policy is None:
                raise capacity_evidence.EvidenceError("production_policy_not_configured")
            bind_scope(policy, os.environ.get("AZURE_SUBSCRIPTION_ID", ""), resource_group, environment_name)
            if not args.retirement_report and (args.skip_quota or args.region):
                raise capacity_evidence.EvidenceError("production_requires_all_pool_quota_reads")
    except capacity_evidence.EvidenceError as exc:
        print(f"ERROR: production capacity preflight: {exc.code}", file=sys.stderr)
        return 2 if args.retirement_report else 1
    if args.retirement_report:
        return run_retirement_report(args, models, catalog_bytes)
    public = retirement.load_public_observations(args.public_evidence, datetime.now(UTC))
    account = active_subscription(os.environ.get("AZURE_SUBSCRIPTION_ID"))
    account_label = f"{account['name']} ({account['id']})" if account["name"] else account["id"]
    print(f"Checking Azure subscription {account_label}.", flush=True)

    by_region = catalog_requirements(
        models,
        include_anthropic=claude_enabled,
        capacity_profile=args.capacity_profile,
    )
    regions = sorted(models["regions"] if args.capacity_profile == "production" else set(args.region or by_region))
    existing_deployments, inventory_warnings = existing_deployment_inventory(
        models,
        resource_group=resource_group,
        environment_name=environment_name,
        foundry_endpoints_raw=os.environ.get("AZURE_FOUNDRY_ENDPOINTS"),
    )
    inventory_at = datetime.now(UTC)
    inventory_state: retirement.InventoryState = (
        "observed" if resource_group or environment_name else "not-requested"
    )
    for warning in inventory_warnings:
        print(f"WARNING: {warning}")
    if existing_deployments:
        print(
            f"Existing-state lifecycle check indexed {len(existing_deployments)} "
            "deployment(s) in the target environment.",
            flush=True,
        )

    total_errors = 0
    total_warnings = 0
    merged_quota: dict[str, dict[str, Any]] = {}
    production_quotas: dict[str, list[dict]] = {}
    for region in regions:
        required = by_region.get(region, [])
        if not required and args.capacity_profile != "production":
            print(f"{region}: no deployments in the catalog; skipping.")
            continue
        print(f"Checking {len(required)} deployments in {region} ...", flush=True)
        offered = offered_models(region)
        scoped_offered = catalog_offerings(required, offered)
        index = index_offered(scoped_offered)
        errors, warnings = evaluate(
            required, index, index_lifecycle(scoped_offered), existing_deployments
        )
        observations = retirement_observations(
            required, offered, existing_deployments, region=region, now=datetime.now(UTC),
            inventory_state=inventory_state, inventory_observed_at=inventory_at, public=public,
        )
        for observation in observations:
            detail = retirement.observation_summary(observation)
            unsafe_date = any(
                evidence.unsafe and evidence.field != "model.lifecycleStatus"
                for evidence in observation.catalog_evidence
            )
            if unsafe_date and observation.decision == "block-addition-or-change":
                errors.append(detail + " Authoritative desired-target date is within the 7-day admission horizon.")
            elif observation.incomplete or observation.attention:
                warnings.append(detail)
            else:
                print(f"  RETIREMENT: {detail}")
        if not args.skip_quota:
            raw_quota = quota_usage(region)
            if args.capacity_profile == "production":
                try:
                    production_quotas[region] = capacity_evidence.parse_quota({"value": raw_quota})
                except capacity_evidence.EvidenceError as exc:
                    errors.append(f"Production quota evidence is unavailable: {exc.code}")
            quota_index = index_quota(raw_quota) if args.capacity_profile != "production" else {}
            # Counters are subscription-wide and identical in every region, so
            # merging is safe; a region that does not offer a model can still be
            # missing its counter, which is why this merges instead of picking one.
            for key, entry in quota_index.items():
                merged_quota.setdefault(key, entry)
            quota_errors, quota_warnings = evaluate_quota(
                required, quota_index, existing_deployments
            ) if args.capacity_profile != "production" else ([], [])
            errors += quota_errors
            warnings += quota_warnings
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
            scope = "available; asserted pool check follows" if args.capacity_profile == "production" else (
                "available" if args.skip_quota else "available and within quota"
            )
            print(f"  all {len(required)} deployments are deployable ({scope}).")
        total_errors += len(errors)
        total_warnings += len(warnings)

    if args.capacity_profile == "production" and policy is not None:
        try:
            for pool in check_live_pools(
                policy, existing_deployments, production_quotas, include_anthropic=claude_enabled,
            ):
                print(
                    f"Production pool {pool['id']} [{pool['counter']}, {pool['scope']}, "
                    f"{pool['unit']}; operator_asserted]: selected {pool['proposedCatalogAllocation']}; "
                    f"headroom {pool['headroomAfter']}; reserved {sum(pool['reserve'].values())}; "
                    f"unreserved {pool['unreservedHeadroomAfter']}. Platform availability is not guaranteed."
                )
        except capacity_evidence.EvidenceError as exc:
            print(f"ERROR: production capacity preflight: {exc.code}")
            total_errors += 1

    if merged_quota:
        # Deliberately evaluated over the whole catalog, not just `regions`: the
        # shared pool is drawn down by every region's deployments regardless of
        # which one the caller asked about, so narrowing it would hide the
        # overcommit that `--region` was used to investigate.
        if args.capacity_profile == "maximum":
            shared_errors, shared_warnings = evaluate_declared_capacity_pools(
                by_region, merged_quota
            )
            fallback = {
                region: [
                    item
                    for item in required
                    if not item.get("capacityPool")
                ]
                for region, required in by_region.items()
            }
            fallback_errors, fallback_warnings = evaluate_shared_quota(
                fallback, merged_quota, existing_deployments
            )
            shared_errors += fallback_errors
            shared_warnings += fallback_warnings
        else:
            shared_errors, shared_warnings = evaluate_shared_quota(
                by_region, merged_quota, existing_deployments
            )
        if shared_errors or shared_warnings:
            print("\nSubscription-wide quota (shared across regions) ...")
            for warning in shared_warnings:
                print(f"  WARNING: {warning}")
            for error in shared_errors:
                print(f"  ERROR: {error}")
        total_errors += len(shared_errors)
        total_warnings += len(shared_warnings)

    print(
        f"\n{total_errors} blocking problem(s), {total_warnings} warning(s) "
        f"across {len(regions)} region(s).",
        flush=True,
    )
    if total_errors:
        print(
            "\nProvisioning will fail on the blocking problems above. Either request "
            "access to (or quota for) the model in this subscription, or adjust its "
            "deployment in infra/models.json and re-run "
            "`python scripts/gen-model-catalog.py`.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
