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

### APIM Basic v2 cutover posture

`gateway.bicep` retains the original Consumption APIM and every one of its child
resources as an **inactive rollback plane**. It is not deleted or repurposed. The
active deterministic `apim-v2-<workload>-<environmentName>` service is Basic v2
(capacity 1, system-assigned identity) and is fully populated with the catalog
named values, content-addressed HTTP/SSE policy fragments, model API/operations,
scoped subscriptions, diagnostics, and Foundry RBAC before either Container App
caller can change. SimpleL7Proxy then uses the replacement model subscription;
FastAPI uses an opaque proxy-ingress key plus a different realtime-only key.

The replacement `openai-realtime` API is an APIM WebSocket API (`wss` backend),
not a synthetic GET operation. Its generated onHandshake policy selects only
catalog endpoints, preserves the request query, strips caller credentials, adds
correlation/managed identity, and returns status-only rejection for an unknown
deployment. Basic v2 has a fixed cost while active. Rollback is an operator-led
caller rewire to the still-present Consumption service; deleting the legacy plane
is intentionally deferred to a separately approved operation.

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

## APIM Basic v2 cutover posture

`gateway.bicep` retains the original Consumption APIM and every child resource as an **inactive rollback plane**. It is not deleted or repurposed. The active deterministic `apim-v2-<workload>-<environmentName>` service is Basic v2 (capacity 1, system-assigned identity) and is fully populated with catalog named values, content-addressed HTTP/SSE policy fragments, model API/operations, scoped subscriptions, diagnostics, and Foundry RBAC before either Container App caller changes. SimpleL7Proxy uses the replacement model subscription; FastAPI uses an opaque proxy-ingress key plus a different realtime-only key.

The replacement `openai-realtime` API is an APIM WebSocket API (`wss` backend), not a synthetic GET operation. Its generated onHandshake policy selects only catalog endpoints, preserves query parameters, strips caller credentials, adds correlation/managed identity, and returns status-only rejection for an unknown deployment. Basic v2 has a fixed cost while active. Rollback is an operator-led caller rewire to the still-present Consumption service; deleting the legacy plane is deferred to a separately approved operation.
