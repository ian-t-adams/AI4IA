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
| `monitoring.bicep` | Log Analytics and workspace-based Application Insights |
| `network.bicep` / `privateendpoints.bicep` | Partial VNet/private-endpoint design scaffolding; not a served end-to-end isolation mode |
| `keyvault.bicep` | Key Vault and App Configuration RBAC; postprovision reconciles the label-aware warm sentinel with the deployment identity |
| `foundry.bicep` / `models.bicep` | Foundry accounts/projects and catalog-driven deployments |
| `data.bicep` | Cosmos (canonical state) plus document/media/blob containers; PostgreSQL was retired and deleted |
| `search.bicep` | Azure AI Search service and RBAC |
| `containerapps.bicep` | Container Apps environment and ACR |
| `api.bicep` / `web.bicep` | API and web Container Apps |
| `gateway.bicep` | Public SimpleL7Proxy HTTP/SSE edge, APIM model/realtime APIs, policy, auth, and Foundry RBAC |
| `proxyasync.bicep` | Default-off AVM Blob + Service Bus backing for durable proxy async jobs |
| `eventhubs.bicep` | Default-off proxy metadata Event Hubs namespace/hub |
| `cost.bicep` | Budget tracking |

Normal model traffic is DNS/custom domain -> SimpleL7Proxy -> APIM -> Foundry.
The FastAPI Voice Live relay bypasses SimpleL7Proxy and uses the separately scoped
APIM realtime API because the proxy does not support WebSockets.

### Shared APIM cutover posture

`apimcore.bicep` owns the unconditional `apim-mcp-<workload>-<environmentName>-<uniqueSuffix>`
Basic v2 service (capacity 1), its system identity, and its sole diagnostic setting.
`gateway.bicep` references it as existing and adds catalog model/realtime APIs, scoped
subscriptions, policy fragments, and Foundry RBAC. The official MCP APIs are feature-gated
inside `mcpgateway.bicep`; their product-scoped key is associated only with MCP APIs, so it cannot
call `openai` or `openai/realtime` after consolidation. SimpleL7Proxy holds the model key;
FastAPI holds distinct opaque proxy-ingress and realtime keys.

The `-<uniqueSuffix>` is load-bearing, not cosmetic. APIM, API Center, and Foundry account
names are unique across all of Azure, so the original unsuffixed names could only ever be
deployed by the one subscription that already held them; standing the stack up in a new
subscription failed with `ServiceAlreadyExists`. `scripts/tests/test_bicep_naming.py` now
pins every globally unique name.

`apim-mcp-*` is the only APIM service in the environment; the original Consumption APIM
and every child were deleted once the Basic v2 plane was proven. The tradeoff is a shared
gateway blast radius: capacity, health, and resiliency monitoring now protect MCP,
HTTP/SSE, and Voice Live together. Rewire callers only after the shared APIs/RBAC are ready.

Regenerate and validate policy routing after any `models.json` change:

```powershell
python scripts/gen-gateway-policy.py
python scripts/gen-gateway-policy.py --check
python -m unittest scripts.tests.test_gateway_policy
```

The direct Bicep `vnetIsolationEnabled` / `dataTierPrivate` parameters are not
present in `main.parameters.json` or normal azd/CI mapping. Their current endpoint
graph is partial: ACR, App Configuration, Search, Foundry, APIM, and monitoring
are not privately covered. Do not treat direct parameter invocation as a supported
private/regulated deployment. The underlying modules remain design scaffolding
until a complete endpoint/DNS matrix and isolated cold-deploy test exist.

## Usage

```powershell
az login
azd env new ai4ia-dev
azd up
```

The azd `preprovision` hook runs catalog/feature validation and the live model
availability, lifecycle, and quota check before submitting ARM. Azure CLI must be
logged into the same `AZURE_SUBSCRIPTION_ID`; an absent or mismatched credential
fails precisely rather than allowing a partial paid/shared-resource deployment.
Lifecycle checks inventory the target Foundry deployments: an exact existing
`Succeeded` deployment warns and reconciles, while greenfield/absent/drifted records block.
Postprovision then hard-gates model state, gateway topology, App Configuration, and
enabled Content Understanding defaults.

Validate in a separate subscription for full catalog fidelity, or use the
runbook's explicitly reduced validation profile when subscription-wide model
quota prevents a duplicate catalog; see
[`../docs/runbooks/teardown.md`](../docs/runbooks/teardown.md).
