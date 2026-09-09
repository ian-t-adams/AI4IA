# Deploy AI4IA to Azure

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](#guided-deployment)

This is a guided deployment rather than a raw ARM-template launch. AI4IA needs
subscription and model quota preflight, Entra app registrations, GitHub OIDC,
container image promotion, postprovision data-plane reconciliation, and
post-deploy verification. A normal Azure portal **Deploy to Azure** button can
submit an ARM template, but it cannot perform those steps; presenting one as a
complete deployment would leave placeholder Container Apps or fail late after
paid resources were created.

Microsoft documents the ARM-only button contract at
[Create a Deploy to Azure button](https://learn.microsoft.com/azure/azure-resource-manager/templates/deploy-to-azure-button).
This repository deliberately links the familiar button to the complete workflow
instead.

## Guided deployment

1. Fork the repository so the deployment workflow and its protected environment
   belong to you.
2. Complete the tool, subscription, quota, identity, and cost prerequisites in
   [Greenfield Azure standup](./greenfield-standup.md).
3. Keep `AI4IA_MODEL_CAPACITY_PROFILE=baseline` for the first deployment. The
   portable baseline is designed to fit more subscriptions than this repository's
   maximum profile.
4. Configure the required repository variables and environment secrets, then run
   **Actions -> deploy -> Run workflow** with **provision** enabled.
5. Complete the first-release data-plane and custom-domain phases in the
   greenfield guide, then verify the exact image digests and authenticated model
   canary.

## Read-only capacity and usage evidence

Use `scripts/report-model-capacity.py` before making a sizing or reserve proposal.
It observes the existing environment; it does not select a profile, identify
production-critical models, recommend downsizing/removal, or change Azure or the
catalog. In particular, it never calls the maximum-capacity planner.

Use an **existing Azure CLI login** with read access to the target resource group,
Cognitive Services quota/model-capacity metadata and Azure Monitor metrics. The
collector does not sign in, select an active subscription, register providers,
create resources, grant permissions or invoke any model. Every read explicitly
names the expected subscription; the CLI's previously selected subscription is
not the target-selection authority.

```powershell
python scripts\report-model-capacity.py `
  --subscription <subscription-guid> `
  --resource-group <exact-resource-group> `
  --environment-name <azd-environment> `
  --days 1 `
  --format json

# Optional: also save the same report to a NEW file in an existing local directory.
python scripts\report-model-capacity.py `
  --subscription <subscription-guid> `
  --resource-group <exact-resource-group> `
  --environment-name <azd-environment> `
  --days 7 `
  --format json `
  --output .azure\capacity-evidence-<utc-date>.json
```

Text is the default format; JSON retains every bounded counter observation and
platform-availability row. `--output` uses exclusive creation and refuses an
existing file or symlink instead of overwriting it. No directory is created.
Reports contain resource names and aggregate operational evidence; review them
before sharing. Subscription GUIDs/resource IDs, credentials, arbitrary tags,
CLI output and provider exception messages are not rendered. A SHA-256
subscription fingerprint binds the report without publishing that GUID.

**Exit 2 means partial/unknown evidence or an input/output failure, not "unused".**
Without independently reviewed pool assertions, exit 2 is expected even when
allocation and metric observations succeeded. Exit 0 requires complete requested
observations and consistent operator-asserted pool evidence; it is not a
deployment/admission gate, an Azure certification, or approval to change capacity.

### What the report measures

| Evidence | Interpretation and limits |
| --- | --- |
| Catalog | File SHA-256, desired model/version/region/SKU, baseline and optional maximum; declaration dates are not recorded, and `maxCapacityPool` remains unverified |
| Live allocation | Exact RG and `env`/`azd-env-name`/`managedBy` ownership, AIServices account naming and resource IDs; actual model/version/SKU/capacity and provisioning state are retained separately from catalog intent |
| Quota counters | Raw `name.value`, `currentValue`, `limit` and `unit`, grouped with their regional observations but **not summed**; subscription usage may include other applications, reservations or versions |
| Platform capacity | Version-specific `modelCapacities` rows for catalog regions, with raw `availableCapacity`; availability is neither usage nor an independently additive quota pool |
| Usage | Only `ModelRequests`, `InputTokens`, `OutputTokens`, `TotalTokens`, queried with hourly `Total` and deployment/model/version/region dimensions |
| Dates | UTC request window, per-source retrieval start/finish and coverage; retrieval time is not a provider-guaranteed atomic allocation snapshot |

`--days` accepts 1-7 whole 24-hour periods. By default the exclusive end is the
previous whole UTC hour, leaving at least an hour for ingestion. An optional
`--end` must be a whole-hour UTC timestamp, no later than that default and no more
than 24 hours older. The tool rejects malformed, future, timezone-free and
out-of-window samples, coarsened response intervals, duplicate samples/series,
wrong model versions and metric-level failures.

`total: 0` requires actual zero-valued samples covering all hourly buckets of
the returned series. No series, no samples or null samples never becomes zero.
Sparse observations retain `observedTotal`, sample/zero counts and observed
peak-hour count, but `total` remains null. A CLI warning or incomplete pagination
is partial, even with usable rows.
Metrics cover **returned series**, not proof of complete workload or billing
coverage. Hourly counts do not establish peak-minute demand, token rate limits,
production importance or spare throughput.

**Account-list continuation is the only paging exception.** Even an ordinary
three-account inventory can have a populated first page and an empty terminal
page. The collector requires that terminal page before trusting inventory. Each
continuation must use HTTPS `management.azure.com`, the exact expected
subscription/resource-group account-list path (case-insensitive ARM identity),
the same API version, and exactly `api-version` plus lowercase `$skiptoken`.
These are the observed service query keys; alternate keys, duplicate parameters,
credentials, ports, fragments, foreign paths, malformed encodings and oversized
links/cursors are refused. The request URL is rebuilt from the fixed target and
validated parameters, not forwarded from server metadata.

Pages share the existing total call/byte/time budgets and the 64-account row
ceiling. Repeated decoded cursors, duplicate account names/IDs, cross-page
ownership ambiguity and page-limit exhaustion prevent a verified inventory.
Only a complete, consistent page set can enable deployment/metric reads.
On refusal or failure, `sources[].pages` retains bounded, non-authoritative
account candidates, row counts, times and codes from safe pages; it never
contains cursors, continuation URLs or subscription-bearing IDs. Text output
also identifies those candidates. Other operations still do not follow
continuations and retain `pagination_not_followed` partial coverage.

Legacy Azure OpenAI metric aliases are not summed with the canonical family.
Dimension **key** casing is handled separately: definitions use
`ModelDeploymentName`, `ModelName`, `ModelVersion`, `Region`; ARM timeseries
metadata can instead use exactly `modeldeploymentname`, `modelname`,
`modelversion`, `region`. Both the CLI projection and parser explicitly map
these aliases to the canonical names. The original unfiltered dimension count
is retained, so unknown extra keys and duplicate/colliding aliases remain
partial instead of being silently dropped. Identity **values** are never
lowercased; model, deployment, version and resource-scope checks still apply.
Voice Live/service-specific metrics lacking the required deployment identity are
explicitly excluded, as are metrics for deleted or uncatalogued deployments.
Unexpected deployments in verified accounts remain separate allocation
observations, never silently part of catalog allocation. Missing catalog entries
are reported even if a feature may intentionally be disabled; the reporter does
not infer the environment's enabled-feature posture.

No prompts, replies, user IDs, tools, logs or traces are requested. Pricing and
dollar cost remain unknown. Raw deployment capacity and quota `Count` are **not
TPM**: conversions are model-specific, and this tool does not invent one.

### Optional reviewed quota-pool assertions

The management APIs do not supply a generally authoritative cross-region quota
pool ID. Equal counters, `available + allocated == limit`, an `AIServices`
publisher label or `GlobalStandard` processing geography do not prove a pool's
scope. The collector therefore leaves headroom unknown by default rather than
copying the maximum planner's heuristics.

An operator can provide a small JSON file with `--pool-evidence <local-file>`.
It records a **separate, dated scope/unit review**, not reserve policy or capacity
targets. The file must contain exactly these fields; replace the illustrative
values with reviewed evidence, not just numbers that happen to fit:

```json
{
  "schemaVersion": 1,
  "subscriptionId": "<expected-subscription-guid>",
  "observedAt": "<UTC-review-time>",
  "reference": "review-2026-09-09-001",
  "pools": [
    {
      "counter": "<exact-live-counter-name>",
      "unit": "Count",
      "scope": "global",
      "regions": ["eastus2", "swedencentral", "westus"],
      "model": {
        "format": "<exact-catalog-format>",
        "name": "<exact-catalog-model>",
        "sku": "<exact-catalog-sku>",
        "versions": ["<every-catalog-version-in-this-pool>"]
      },
      "capacityUnitsPerCounterUnit": 1
    }
  ]
}
```

The subscription must match exactly, and `observedAt` must be UTC, not future,
and at most 24 hours old **when collection finishes**. `reference` is a bounded
review ID containing only letters, digits, dot, underscore and hyphen, not a URL
or a person's identity. The file hash and review time remain in the report.
Never put secrets or approval tokens in this file.

Scopes are `global`, `region:<catalog-region>`, or
`data-zone:<catalog-dataZone>`. `regions` must contain every catalog region in
that asserted scope, without omissions/duplicates. Model format/name/SKU and
every catalog version in the pool must agree. Counter membership cannot overlap
another assertion, even if it uses a different version or unit label.
`capacityUnitsPerCounterUnit: 1` is an explicit operator assertion that these raw
units are comparable for this exact model/SKU/counter, not an automatic
conversion. Other conversion factors are deliberately unsupported.

For a consistent assertion, the report uses the shared counter **once**,
subtracts its `currentValue` from its limit, and separately shows matched catalog
allocation, `knownUncataloguedAllocation` and
`outsideCatalogOrUnattributedAllocation`. The latter includes known uncatalogued
allocation; these are not independent quantities to add together. It is not
available quota and is not claimed to belong to another known application.
Inconsistent replicas, missing coverage, identity/version/state drift, warnings,
stale assertions or contradictory allocation/platform data leave headroom null.
Known successful uncatalogued allocations also participate in the consistency
check; the counter cannot report less than the allocations already observed.
An unsettled or unreviewed-version deployment in that pool leaves headroom unknown.
Version-specific platform availability is retained, not summed; deployable
headroom remains unknown. Independent regional pools and unlike model units are
never combined into a grand capacity/headroom total.

All pool arithmetic is labeled `operator_asserted`. No supplied file changes
capacity, enables a production profile, establishes production criticality or
chooses replacement/workload reserves. Those remain separately approved work.

### Collection bounds and incomplete evidence

| Bound | Ceiling |
| --- | --- |
| Azure process / read-collection budget | 20 seconds per process; 300 seconds across reads, no retry loop |
| Metadata scope | 8 catalog regions, 64 account inventory rows, 256 deployments total, 128 distinct model versions |
| Account-list continuation | 64 pages, 8 KiB ASCII next-link and 4 KiB decoded ASCII cursor; existing total read budgets still apply |
| Metadata rows / bytes | 2,048 rows per metadata source; 8 MiB per response, 64 MiB across responses |
| Metric samples | 256 series per metric, 168 hourly points per series, 200,000 points across collection |
| Local inputs / output | 2 MiB catalog, 128 KiB pool assertions, 2 MiB final escaped report |
| Requests | 192 fixed, subscription-scoped ARM GET calls at most |

Metric requests ask for one extra series as an overflow sentinel. A truncated
series response cannot pass as complete. Missing or malformed data and exhausted
budgets have fixed per-source codes; healthy unrelated observations remain
readable. Oversized final output becomes a small explicit unknown/error report,
never a truncated successful report. No live collection runs on pull requests;
the quality job uses fixtures and a mocked Azure read transport.

The offline capacity tests require the pinned `jmespath==0.9.5` parser used by
the inspected Azure CLI. They execute the real projection on raw ARM fixtures,
including the 15-series/360-point lowercase-key case and its TitleCase control.
The reporter gains no Python runtime dependency and CI makes no Azure calls.

The byte budget counts stdout payload bytes on failed reads too. An overflow
uses at most one detection byte beyond the remaining allowance; diagnostics have
a separate 64 KiB per-process ceiling and are never rendered. If a failed
transport cannot report its byte count, the whole allowance is conservatively
reserved. `consumed.responseBudgetBytes` is therefore budget consumption, not a
claim about the wire size of Azure responses.

API references:
[quota and raw capacity units](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/quota),
[bounded metric filters and aggregation](https://learn.microsoft.com/rest/api/monitor/metrics/list?view=rest-monitor-2023-10-01).

## Use all available model capacity

After the baseline deployment exists, generate a maximum profile for that
subscription:

```powershell
python scripts/sync-model-capacity.py `
  --subscription <subscription-guid> `
  --resource-group <resource-group> `
  --environment-name <azd-environment> `
  --output-plan .azure/model-capacity-plan.json

# Review the plan, then record it in IaC.
python scripts/sync-model-capacity.py `
  --subscription <subscription-guid> `
  --resource-group <resource-group> `
  --environment-name <azd-environment> `
  --apply
```

The planner reads existing deployments and uses quota/`modelCapacities` numbers,
SKU and publisher heuristics to choose global, data-zone or regional pools.
Those heuristics and a recorded `maxCapacityPool` are not independent evidence
that the same scope holds in another subscription or at another time. It never
reduces a live deployment; that is not proof that its maximum is a safe production
reserve policy. Use the read-only evidence report and a separate scope review
before approving any capacity change.

Commit the resulting `infra/models.json`, set the repository variable
`AI4IA_MODEL_CAPACITY_PROFILE=maximum`, and run the deploy workflow with
**provision** enabled. Maximum capacity is subscription-specific and can consume
all quota for those model pools, leaving no headroom for another application or
concurrent deployment. Standard token-per-minute capacity is still billed by
actual model usage; this profile does not create Provisioned Throughput Units.
