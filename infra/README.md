# infra — Azure infrastructure

`azd` provisions AI4IA from [`main.bicep`](./main.bicep) at subscription scope.
The deployment creates the resource group, shared tags, identities, observability,
data services, model deployments, Container Apps, APIM/SimpleL7Proxy, and optional
feature resources.

## Files

- `main.bicep` — root deployment and module wiring.
- `main.parameters.json` — azd parameter bindings from `AZURE_*` and `AI4IA_*`
  environment values.
- `models.json` — model deployment source of truth.
- `models.schema.json` — schema for `models.json`.
- `abbreviations.json` — resource-name abbreviations.
- `modules/` — Bicep modules consumed by `main.bicep`.

## Modules

| Module | Purpose |
|---|---|
| `identity.bicep` | User-assigned managed identities |
| `monitoring.bicep` | Log Analytics, Application Insights, Azure Monitor workspace |
| `network.bicep` / `privateendpoints.bicep` | Optional VNet/private-endpoint isolation |
| `keyvault.bicep` | Key Vault and App Configuration |
| `foundry.bicep` / `models.bicep` | Foundry accounts/projects and catalog-driven deployments |
| `data.bicep` | Cosmos, Postgres/pgvector, document/media/blob containers |
| `search.bicep` | Azure AI Search service and RBAC |
| `containerapps.bicep` | Container Apps environment and ACR |
| `api.bicep` / `web.bicep` | API and web Container Apps |
| `gateway.bicep` | SimpleL7Proxy Container App and APIM gateway |
| `eventhubs.bicep` | Telemetry Event Hubs namespace/hub |
| `cost.bicep` | Budget tracking |

## Usage

```powershell
az login
azd env new ai4ia-dev
azd up
```

Validate in a parallel resource group before replacing a live stack; see
[`../docs/runbooks/teardown.md`](../docs/runbooks/teardown.md).
