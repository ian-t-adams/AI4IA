# Runbook: Teardown & Rebuild

> **Destructive.** This is the break-glass teardown path for the live `slurmfactory`
> AI4IA environment. Follow the order exactly. Validate in a parallel RG **before** deleting anything.

## Context
- Subscription: `ca68cf94-f445-43f1-8379-3d0100e293a2`
- Tenant: `nomad-analytics`
- Live stack RG: `rg-ai4ia-slurmfactory`
- **Protected (never delete):** `NetworkWatcherRG`, `Default-ActivityLogAlerts`,
  `DefaultResourceGroup-*`
- The helper script still defaults to the legacy pre-AI4IA `rg-aiforia-slurmfactory`
  target, so pass `-ResourceGroups` and `-PurgeNameFilter` explicitly for the live stack.

## 0. Pre-flight (read-only)
```powershell
az login
./scripts/inventory.ps1 -Subscription ca68cf94-f445-43f1-8379-3d0100e293a2 `
  -ResourceGroup rg-ai4ia-slurmfactory
```
Archive the resulting summary outside the target resource group so the rebuild is auditable and reversible.

## 1. Validate IaC in a PARALLEL resource group (no deletion yet)
```powershell
azd env new ai4ia-validate
azd env set AZURE_RESOURCE_GROUP rg-ai4ia-validate
azd provision
```
Confirm Foundry accounts, model deployments, and core services come up cleanly and that
quota/capacity is sufficient in eastus2 + swedencentral + westus. Fix IaC until green.

## 2. Tear down the live stack
```powershell
# Dry run first (lists resources, deletes nothing):
./scripts/teardown.ps1 -Subscription ca68cf94-f445-43f1-8379-3d0100e293a2 `
  -ResourceGroups rg-ai4ia-slurmfactory -PurgeNameFilter ai4ia

# Execute:
./scripts/teardown.ps1 -Subscription ca68cf94-f445-43f1-8379-3d0100e293a2 `
  -ResourceGroups rg-ai4ia-slurmfactory -PurgeNameFilter ai4ia -Force
```
This deletes `rg-ai4ia-slurmfactory` and purges soft-deleted Cognitive/Key Vault resources
matching `ai4ia`.

## 3. Provision the real environment
```powershell
azd env select slurmfactory   # or azd env new slurmfactory in a fresh checkout
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
