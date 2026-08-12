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
"""

from __future__ import annotations

import argparse
import json
import os
import re
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


def catalog_requirements(models: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Group desired deployment records by region, including their exact ARM names."""
    naming = models.get("naming") or {}
    pattern = str(
        naming.get("pattern")
        or "{model}-{subscriptionToken}-{region}-{skuShort}"
    )
    subscription_token = str(naming.get("subscriptionToken") or "")
    sku_short = naming.get("skuShort") or {}
    by_region: dict[str, list[dict[str, Any]]] = {}
    for entry in models.get("catalog", []):
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
                    "capacity": deployment.get("capacity", 0),
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
    try:
        return json.loads(result.stdout or "[]")
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
    for entry in entries if isinstance(entries, list) else []:
        region = _normal_location(entry.get("region"))
        account_name = str(entry.get("accountName") or "").strip()
        if not region or not account_name:
            continue
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
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
    """Inventory exact deployments azd would reconcile, or enter explicit addition mode.

    No context means a manual/greenfield check: every desired deployment is treated
    as an addition. In an azd hook the target resource group/environment are known;
    any Azure inventory error fails rather than incorrectly exempting a lifecycle
    block. All operations are read-only.
    """
    resource_group = (resource_group or "").strip()
    environment_name = (environment_name or "").strip()
    if not resource_group and environment_name:
        workload = (os.environ.get("AI4IA_WORKLOAD") or "ai4ia").strip()
        resource_group = f"rg-{workload}-{environment_name}"
    if not resource_group:
        return {}, [
            "No target resource group/environment was supplied; lifecycle checking "
            "is in greenfield/addition mode, so deprecated or deprecating desired "
            "deployments remain blocking. Set AZURE_ENV_NAME (or --resource-group "
            "and --environment-name) to evaluate an existing routine reconcile."
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
        return {}, [
            f"Target resource group {resource_group} does not exist; lifecycle checking "
            "is in greenfield/addition mode."
        ]

    accounts = _json_result(
        _az("cognitiveservices", "account", "list", "--resource-group", resource_group, "-o", "json"),
        f"could not list Cognitive Services accounts in {resource_group}",
    )
    if not isinstance(accounts, list):
        raise SystemExit(
            f"ERROR: Cognitive Services account inventory for {resource_group} was not an array."
        )
    explicit_accounts = _foundry_accounts_from_output(foundry_endpoints_raw)
    foundry_token = str((models.get("naming") or {}).get("foundryToken") or "")
    inventory: dict[tuple[str, str], dict[str, Any]] = {}

    for region in (models.get("regions") or {}):
        normalized_region = _normal_location(region)
        expected_name = explicit_accounts.get(normalized_region)
        if expected_name:
            candidates = [
                account
                for account in accounts
                if str(account.get("name") or "").casefold() == expected_name.casefold()
            ]
        elif environment_name:
            prefix = f"mf-{foundry_token}-{environment_name}-{region}-".casefold()
            candidates = [
                account
                for account in accounts
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
            continue
        account_name = str(candidates[0].get("name") or "")
        deployments = _json_result(
            _az(
                "cognitiveservices",
                "account",
                "deployment",
                "list",
                "--resource-group",
                resource_group,
                "--name",
                account_name,
                "-o",
                "json",
            ),
            f"could not list model deployments for {account_name}",
        )
        if not isinstance(deployments, list):
            raise SystemExit(
                f"ERROR: deployment inventory for {account_name} was not an array."
            )
        for deployment in deployments:
            deployment_name = str(deployment.get("name") or "").strip()
            if not deployment_name:
                continue
            properties = deployment.get("properties") or {}
            model = properties.get("model") or {}
            sku = deployment.get("sku") or {}
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
    return inventory, []


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
    try:
        actual_capacity = int(existing.get("capacity"))
    except (TypeError, ValueError):
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
    result = _az("cognitiveservices", "model", "list", "--location", region, "-o", "json")
    if result.returncode != 0:
        raise SystemExit(
            f"ERROR: could not list models in {region}. Run `az login` and "
            f"`az account set --subscription <id>` first.\n{result.stderr.strip()}"
        )
    return json.loads(result.stdout or "[]")


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
        index.setdefault(name.casefold(), {})[str(model.get("version", ""))] = str(status)
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
                    "treating as deployable. Check whether it blocks new deployments."
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
    args = parser.parse_args()

    account = active_subscription(os.environ.get("AZURE_SUBSCRIPTION_ID"))
    account_label = f"{account['name']} ({account['id']})" if account["name"] else account["id"]
    print(f"Checking Azure subscription {account_label}.", flush=True)

    models = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    by_region = catalog_requirements(models)
    regions = args.region or sorted(by_region)
    environment_name = args.environment_name or os.environ.get("AZURE_ENV_NAME")
    resource_group = args.resource_group or os.environ.get("AZURE_RESOURCE_GROUP")
    existing_deployments, inventory_warnings = existing_deployment_inventory(
        models,
        resource_group=resource_group,
        environment_name=environment_name,
        foundry_endpoints_raw=os.environ.get("AZURE_FOUNDRY_ENDPOINTS"),
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
    for region in regions:
        required = by_region.get(region, [])
        if not required:
            print(f"{region}: no deployments in the catalog; skipping.")
            continue
        print(f"Checking {len(required)} deployments in {region} ...", flush=True)
        offered = offered_models(region)
        index = index_offered(offered)
        errors, warnings = evaluate(
            required, index, index_lifecycle(offered), existing_deployments
        )
        if not args.skip_quota:
            quota_index = index_quota(quota_usage(region))
            # Counters are subscription-wide and identical in every region, so
            # merging is safe; a region that does not offer a model can still be
            # missing its counter, which is why this merges instead of picking one.
            for key, entry in quota_index.items():
                merged_quota.setdefault(key, entry)
            quota_errors, quota_warnings = evaluate_quota(
                required, quota_index, existing_deployments
            )
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
            scope = "available" if args.skip_quota else "available and within quota"
            print(f"  all {len(required)} deployments are deployable ({scope}).")
        total_errors += len(errors)
        total_warnings += len(warnings)

    if merged_quota:
        # Deliberately evaluated over the whole catalog, not just `regions`: the
        # shared pool is drawn down by every region's deployments regardless of
        # which one the caller asked about, so narrowing it would hide the
        # overcommit that `--region` was used to investigate.
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
