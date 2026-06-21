# Runbook: Teardown & Rebuild

> **Destructive.** This wipes the existing `aiforia` Foundry stack so AI4IA can be rebuilt from
> IaC. Follow the order exactly. Validate in a parallel RG **before** deleting anything.

## Context
- Subscription: `sub-planetexpress-slurmfactory` (`ca68cf94-f445-43f1-8379-3d0100e293a2`)
- Tenant: `6df60a0a-8c74-433a-9d69-513af272d8d4`
- Existing stack RG: `rg-aiforia-slurmfactory` (+ 2 managed RGs)
- **Protected (never delete):** `NetworkWatcherRG`, `Default-ActivityLogAlerts`,
  `DefaultResourceGroup-*`

## 0. Pre-flight (read-only)
```powershell
az login
./scripts/inventory.ps1 -Subscription ca68cf94-f445-43f1-8379-3d0100e293a2 `
  -ResourceGroup rg-aiforia-slurmfactory
```
Commit the resulting summary so the rebuild is auditable and reversible.

## 1. Validate IaC in a PARALLEL resource group (no deletion yet)
```powershell
azd env new ai4ia-validate
azd env set AZURE_RESOURCE_GROUP rg-ai4ia-validate
azd provision
```
Confirm Foundry accounts, model deployments, and core services come up cleanly and that
quota/capacity is sufficient in eastus2 + swedencentral + westus. Fix IaC until green.

## 2. Tear down the old stack
```powershell
# Dry run first (lists resources, deletes nothing):
./scripts/teardown.ps1 -Subscription ca68cf94-f445-43f1-8379-3d0100e293a2

# Execute:
./scripts/teardown.ps1 -Subscription ca68cf94-f445-43f1-8379-3d0100e293a2 -Force
```
This deletes `rg-aiforia-slurmfactory` and purges soft-deleted Cognitive/Key Vault resources
matching `aiforia`.

## 3. Provision the real environment
```powershell
azd env new ai4ia-dev
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
