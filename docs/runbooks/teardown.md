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

## 0. Pre-flight (read-only)
```powershell
./scripts/inventory.ps1 -Subscription $sub -ResourceGroup $rg
```
Archive the resulting summary outside the target resource group so the rebuild is auditable and reversible.

## 1. Validate IaC in a PARALLEL resource group (no deletion yet)
```powershell
azd env new ai4ia-validate
azd env set AZURE_RESOURCE_GROUP rg-ai4ia-validate
azd provision
```
Confirm Foundry accounts, model deployments, and core services come up cleanly and that
quota/capacity is sufficient in every region in `infra/models.json`. Fix IaC until green.

## 2. Tear down the live stack
```powershell
# Dry run first (lists resources, deletes nothing):
./scripts/teardown.ps1 -Subscription $sub -ResourceGroups $rg -PurgeNameFilter ai4ia

# Execute:
./scripts/teardown.ps1 -Subscription $sub -ResourceGroups $rg -PurgeNameFilter ai4ia -Force
```
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
covered in [`deployment.md`](./deployment.md#25-custom-domains-vanity-hostnames--required-if-you-use-them).

## Rollback
There is no in-place rollback after step 2. The inventory snapshot from step 0 plus
`infra/models.json` are the recovery source — re-run `azd provision` to rebuild.
