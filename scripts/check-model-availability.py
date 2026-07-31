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
  model is still listed, still has quota, and still serves existing deployments
  for months or years, so both checks above pass -- but creating a new one fails
  with `ServiceModelDeprecating`. Offered is not the same as deployable.
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

This is an operator preflight, not a CI gate: it needs `az login` and a selected
subscription. Run it before the first provision in a new tenant/subscription.
See docs/runbooks/deployment.md ("Moving to a new subscription or tenant").

Usage:
    az account set --subscription <id>
    python scripts/check-model-availability.py
    python scripts/check-model-availability.py --region eastus2   # narrow it
    python scripts/check-model-availability.py --skip-quota       # availability only
"""

from __future__ import annotations

import argparse
import json
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


def catalog_requirements(models: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Group the catalog's deployments by region.

    Returns {region: [{name, sku, version, capacity}, ...]}.
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
                    "capacity": deployment.get("capacity", 0),
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

    One convention survives that: partner counters drop a ``.0`` version
    suffix, so `Cohere-rerank-v4.0-pro` counts against `Cohere-Rerank-V4-Pro`.
    That gets a second candidate rather than a name-specific special case.
    """

    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.casefold())

    sku_key = norm(sku)
    keys = [f"{sku_key}|{norm(model)}"]
    without_dot_zero = model.replace(".0", "")
    if without_dot_zero != model:
        keys.append(f"{sku_key}|{norm(without_dot_zero)}")
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
    required: list[dict[str, str]], index: dict[str, dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Compare one region's requested capacity against quota.

    Requested capacity is summed per model+SKU: several catalog deployments of
    the same model in one region draw down a single shared counter, so checking
    them individually would let a pair that each fit -- but together do not --
    pass.

    **Only `capacity > limit` is an error here.** That is unarguable: the catalog
    is asking for more than the subscription could ever hold, and no retry helps.

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

    wanted: dict[tuple[str, str], int] = {}
    for item in required:
        key = (item["sku"], item["name"])
        wanted[key] = wanted.get(key, 0) + int(item.get("capacity") or 0)

    for (sku, name), capacity in sorted(wanted.items()):
        entry = next((index[k] for k in _quota_keys(sku, name) if k in index), None)
        if entry is None:
            warnings.append(
                f"{name} ({sku}): no quota counter matched; capacity {capacity} unverified. "
                "Confirm with `az cognitiveservices usage list -l <region>`."
            )
            continue
        limit = entry["limit"]
        if capacity > limit:
            errors.append(
                f"{name} ({sku}): needs {capacity} but the subscription limit is "
                f"{limit:.0f} [{entry['counter']}]. Request a quota increase or lower "
                "`capacity` in infra/models.json."
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
    by_region: dict[str, list[dict[str, str]]], index: dict[str, dict[str, Any]]
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
    and re-reporting would double-count.
    """
    errors: list[str] = []
    warnings: list[str] = []

    totals: dict[tuple[str, str], int] = {}
    regions: dict[tuple[str, str], set[str]] = {}
    for region, required in by_region.items():
        for item in required:
            key = (item["sku"], item["name"])
            totals[key] = totals.get(key, 0) + int(item.get("capacity") or 0)
            regions.setdefault(key, set()).add(region)

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
        if counter.partition(".")[0].casefold() == "openai":
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


# Azure refuses *new* deployments of these, while existing ones keep serving.
UNDEPLOYABLE_LIFECYCLE = frozenset({"deprecating", "deprecated"})
DEPLOYABLE_LIFECYCLE = frozenset({"generallyavailable", "preview"})


def evaluate(
    required: list[dict[str, str]],
    index: dict[str, dict[str, set[str]]],
    lifecycle: dict[str, dict[str, str]],
) -> tuple[list[str], list[str]]:
    """Compare one region's requirements against what it offers.

    Returns (errors, warnings). A missing model or SKU is an error -- the
    deployment cannot succeed. So is a model whose pinned version is
    deprecating/deprecated: Azure lists it, quotas it, and still refuses to
    create a new deployment of it.

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

        status = (lifecycle.get(name.casefold()) or {}).get(version)
        if status:
            folded = status.casefold()
            if folded in UNDEPLOYABLE_LIFECYCLE:
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
                    f"{name} ({version}): lifecycle is {status}; Azure refuses new "
                    f"deployments with ServiceModelDeprecating. Existing deployments "
                    f"keep serving, so this is invisible until a clean provision. {remedy}."
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
    args = parser.parse_args()

    models = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    by_region = catalog_requirements(models)
    regions = args.region or sorted(by_region)

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
        errors, warnings = evaluate(required, index, index_lifecycle(offered))
        if not args.skip_quota:
            quota_index = index_quota(quota_usage(region))
            # Counters are subscription-wide and identical in every region, so
            # merging is safe; a region that does not offer a model can still be
            # missing its counter, which is why this merges instead of picking one.
            for key, entry in quota_index.items():
                merged_quota.setdefault(key, entry)
            quota_errors, quota_warnings = evaluate_quota(required, quota_index)
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
        shared_errors, shared_warnings = evaluate_shared_quota(by_region, merged_quota)
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
