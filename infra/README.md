# infra — Infrastructure as Code (Bicep + azd)

`azd` provisions everything here from [`main.bicep`](./main.bicep) (subscription scope).

## Files
- `main.bicep` — root deployment; creates the RG + tags and wires modules.
- `main.parameters.json` — azd parameter bindings (reads `AZURE_*` env vars).
- `models.json` — **data-driven model catalog** (single source of truth for deployments).
  Generated/validated from live Azure availability; see `scripts/seed-models.ps1`.
- `models.schema.json` — JSON schema for `models.json` (validated in CI).
- `abbreviations.json` — resource-name abbreviations (azd convention).
- `modules/` — composable Bicep modules (added in Phase 1+).

## Planned modules (Phase 1+)
| Module | Purpose |
|---|---|
| `monitoring.bicep` | Log Analytics + App Insights + Azure Monitor workspace |
| `identity.bicep` | User-assigned managed identities + RBAC |
| `network.bicep` | VNet/subnets, private endpoints, private DNS (baseline) |
| `keyvault.bicep` | Key Vault + App Configuration |
| `foundry.bicep` | Foundry (Cognitive) accounts + projects per region |
| `models.bicep` | Iterates `models.json` to create model deployments |
| `data.bicep` | Cosmos DB (NoSQL) + Postgres Flexible (pgvector) |
| `containerapps.bicep` | Container Apps env + apps (web, api, mem0, proxy) + ACR |
| `eventhubs.bicep` | Event Hubs for telemetry/cost streaming |
| `gateway.bicep` | SimpleL7Proxy + APIM (model gateway) |
| `dns.bicep` | Custom-domain records for app + proxy |

## Usage
```powershell
az login
azd env new ai4ia-dev
azd up
```
Validate IaC in a parallel RG before tearing down the existing stack — see
[`docs/runbooks/teardown.md`](../docs/runbooks/teardown.md).
