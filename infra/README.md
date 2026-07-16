# infra — Azure infrastructure

`azd` provisions AI4IA from [`main.bicep`](./main.bicep) at subscription scope.
The deployment creates the resource group, shared tags, identities, observability,
data services, model deployments, Container Apps, SimpleL7Proxy/APIM, and optional
feature resources.

## Files

- `main.bicep` — root deployment and module wiring.
- `main.parameters.json` — azd parameter bindings from `AZURE_*` and `AI4IA_*`
  environment values.
- `models.json` — model deployment source of truth.
- `models.schema.json` — schema for `models.json`.
- `policies/` — upstream-derived APIM retry/requeue policy plus the generated
  catalog endpoint fragment.
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
| `gateway.bicep` | Public SimpleL7Proxy HTTP/SSE edge, APIM model/realtime APIs, policy, auth, and Foundry RBAC |
| `proxyasync.bicep` | Default-off AVM Blob + Service Bus backing for durable proxy async jobs |
| `eventhubs.bicep` | Telemetry Event Hubs namespace/hub |
| `cost.bicep` | Budget tracking |

Normal model traffic is DNS/custom domain -> SimpleL7Proxy -> APIM -> Foundry.
The FastAPI Voice Live relay bypasses SimpleL7Proxy and uses the separately scoped
APIM realtime API because the proxy does not support WebSockets.

### Shared APIM cutover posture

`apimcore.bicep` owns/adopts the unconditional existing `apim-mcp-<workload>-<environmentName>`
Basic v2 service (capacity 1), its system identity, and its sole diagnostic setting.
`gateway.bicep` references it as existing and adds catalog model/realtime APIs, scoped
subscriptions, policy fragments, and Foundry RBAC. The official MCP APIs are feature-gated
inside `mcpgateway.bicep`; their product-scoped key is associated only with MCP APIs, so it cannot
call `openai` or `openai/realtime` after consolidation. SimpleL7Proxy holds the model key;
FastAPI holds distinct opaque proxy-ingress and realtime keys.

The original Consumption APIM and every child remain unchanged as an inactive HTTP/SSE rollback
plane; it receives no active traffic and is not deleted. Reusing `apim-mcp-*` adds no additional
roughly $150/month APIM base charge. The tradeoff is a shared gateway blast radius: capacity,
health, and resiliency monitoring now protect MCP, HTTP/SSE, and Voice Live together. Rewire
callers only after the shared APIs/RBAC are ready. Delete Consumption only in a separately
approved post-stabilization destructive change.

Regenerate and validate policy routing after any `models.json` change:

```powershell
python scripts/gen-gateway-policy.py
python scripts/gen-gateway-policy.py --check
python -m unittest scripts.tests.test_gateway_policy
```

When `dataTierPrivate=true`, the optional async Blob and Service Bus resources
also disable public access and receive private endpoints/DNS. Both send platform
diagnostics to the shared Log Analytics workspace.

## Usage

```powershell
az login
azd env new ai4ia-dev
azd up
```

Validate in a parallel resource group before replacing a live stack; see
[`../docs/runbooks/teardown.md`](../docs/runbooks/teardown.md).
