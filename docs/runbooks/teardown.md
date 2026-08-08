# Runbook: Teardown & Rebuild

> **Destructive.** This is the break-glass teardown path for an AI4IA environment.
> Follow the order exactly. Validate in a parallel RG **before** deleting anything.

## Context

Every command below takes the target explicitly — no script in this repo carries a
default subscription, resource group, or purge filter, because a baked-in target is
silently *wrong* after a move to a new subscription or tenant. Resolve the values for
the environment you are tearing down first:

```powershell
az login
azd env select <env>                        # the azd env for the target stack
$sub = azd env get-value AZURE_SUBSCRIPTION_ID
$rg  = azd env get-value AZURE_RESOURCE_GROUP
```

- Live stack RG: `rg-ai4ia-<env>` (from `AZURE_ENV_NAME`)
- **Protected (never delete):** `NetworkWatcherRG`, `Default-ActivityLogAlerts`,
  `DefaultResourceGroup-*`

## Targeted Lean Azure retained-resource cleanup (one-time)

ARM incremental deployments do not delete resources removed from Bicep or
hidden behind a newly disabled condition. After the Lean Azure migration is
merged and reprovisioned, an authorized operator must separately remove the
retained Event Hubs namespace and its direct RBAC, the retired
`Microsoft.Monitor/accounts` workspace, and the portal-created API Center
`swagger-petstore` sample. This is intentionally not a deploy hook: changing a
feature flag must never delete live Azure.

Resolve and inspect the three exact IDs first:

```powershell
$eventHubsId = az eventhubs namespace show --subscription $sub --resource-group $rg --name <exact-event-hubs-name> --query id -o tsv
$monitorWorkspaceId = az resource show --subscription $sub --resource-group $rg --resource-type Microsoft.Monitor/accounts --name <exact-monitor-workspace-name> --query id -o tsv
$apiCenterId = az resource show --subscription $sub --resource-group $rg --resource-type Microsoft.ApiCenter/services --name <exact-api-center-name> --query id -o tsv
$sampleApiId = "$apiCenterId/workspaces/default/apis/swagger-petstore"
```

Preview the exact-resource migration; this mode makes no Azure CLI calls:

```powershell
./scripts/cleanup-lean-azure-retained.ps1 `
  -EventHubsNamespaceResourceId $eventHubsId `
  -MonitorWorkspaceResourceId $monitorWorkspaceId `
  -ApiCenterSampleApiResourceId $sampleApiId
```

After verifying every printed ID, execute explicitly:

```powershell
./scripts/cleanup-lean-azure-retained.ps1 `
  -EventHubsNamespaceResourceId $eventHubsId `
  -MonitorWorkspaceResourceId $monitorWorkspaceId `
  -ApiCenterSampleApiResourceId $sampleApiId `
  -Execute -AcknowledgeRetainedResourceDeletion
```

The script requires all targets to share one subscription/resource group,
verifies all three before deleting anything, removes only direct role assignments
at the exact Event Hubs namespace scope, then deletes those three resources.
Re-run `azd provision` and `scripts/status-snapshot.ps1` afterward to confirm they
remain absent and refresh the published inventory.

## 0. Pre-flight (read-only)

Two captures, and they cover different things. `inventory.ps1` records the
**infrastructure** so `azd provision` can rebuild it.
`capture-data-recovery-state.ps1` records the **data facts that stop existing when
the resource group does** — the Cosmos restorable-instance id and restore window,
a blob manifest, and Key Vault secret names.

```powershell
./scripts/inventory.ps1 -Subscription $sub -ResourceGroup $rg
./scripts/capture-data-recovery-state.ps1 -Subscription $sub -ResourceGroup $rg
```

Archive both outside the target resource group so the rebuild is auditable and
reversible.

Read the capture summary rather than filing it. Two of its outputs decide whether
step 2 is safe:

- **`Blob: SIZE UNKNOWN`** means the probe could not read a container, not that
  the container is empty. Listing blobs needs data-plane rights (Storage Blob
  Data Reader); subscription Owner alone is not enough. Re-run with those rights
  before treating "no blobs" as true.
- **`secret names NOT captured`** means you will not know which users to tell that
  their MCP credentials were purged.

## 1. Validate IaC in a PARALLEL resource group (no deletion yet)
```powershell
azd env new ai4ia-validate
azd env set AZURE_RESOURCE_GROUP rg-ai4ia-validate
azd provision
```
Confirm Foundry accounts, model deployments, and core services come up cleanly and that
quota/capacity is sufficient in every region in `infra/models.json`. Fix IaC until green.

## 2. Tear down the live stack

> **Stop.** This step is irreversible for data, not just for infrastructure. It
> deletes canonical Cosmos state and uploaded blobs, and **purges** the Key Vault
> holding per-user MCP credentials. Read [Rollback](#rollback) below and run the
> step 0 capture first.

```powershell
# Dry run first (lists resources, deletes nothing):
./scripts/teardown.ps1 -Subscription $sub -ResourceGroups $rg -PurgeNameFilter ai4ia

# Execute:
./scripts/teardown.ps1 -Subscription $sub -ResourceGroups $rg -PurgeNameFilter ai4ia -Force -AcknowledgeDataLoss
```

`-Force` alone is refused (exit 2). `-Force` only ever acknowledged deleting the
**infrastructure**, which this repo can rebuild; `-AcknowledgeDataLoss`
acknowledges the part it cannot. Keeping them separate stops the irreversible
acknowledgement from riding along with the routine one.

This deletes the resource group and purges soft-deleted Cognitive/Key Vault resources
matching the filter. The purge lists are **subscription-wide**, so choose a filter that
matches only this stack — `-PurgeNameFilter` is mandatory for exactly that reason.

## 3. Provision the real environment
```powershell
azd env select <env>   # or azd env new <env> in a fresh checkout
azd up
```

## 4. Post-provision
```powershell
./scripts/postprovision.ps1
```
Then verify any external DNS records for custom domains and run smoke tests. DNS
records are managed outside this repo; the Azure-side custom-domain binding is
covered in the
[greenfield standup guide](./greenfield-standup.md#62-bind-custom-domains-optional).

## Rollback
There is no in-place rollback after step 2. The inventory snapshot from step 0 plus
`infra/models.json` rebuild the **infrastructure** — re-run `azd provision`.

> **They do not rebuild your data.** Neither input contains a single session,
> message, memory, usage row, user agent/workflow, uploaded document or secret.
> Read this before step 2, not after it:
>
> - **Cosmos (sessions, messages, usage, memory, agents, workflows, document
>   manifests)** has continuous backup with point-in-time restore, and the
>   procedure is tested — see
>   [`deployment.md`](./deployment.md#5-data-recovery-posture).
>   Restore targets a **new account** and is addressed by the restorable-instance
>   id, not the account name, so the step 0 capture records that id, the location
>   and the window. After deletion those are no longer queryable.
> - **Blob (uploaded source documents, generated images/videos)** has no restore
>   path here. The step 0 capture lists what is there; export anything you need to
>   keep, outside the target resource group, before step 2.
> - **Key Vault (per-user BYO MCP credentials)** is soft-deleted, and step 2
>   **purges** it. Purged secrets are unrecoverable; users must re-enter them.
> - **Derived stores** (document chunks, the AI Search index, parsed artifacts)
>   are intentionally rebuildable and need no backup.
>
> Treat "rebuild the environment, keep the data" and "dispose of the environment"
> as two different operations. Only the second one is what this runbook does.
