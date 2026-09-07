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
- `mcp-servers.json` / `mcp-servers.schema.json` — official MCP inventory.
- `voice-providers.json` / `voice-providers.schema.json` — realtime provider catalog.
- `proxy-container-config.json` — reviewed SimpleL7Proxy logging posture.
- `policies/` — hand-authored APIM policy sources plus generated catalog shards.
- `abbreviations.json` — reference shorthand; actual names come from Bicep expressions.
- `modules/` — Bicep modules consumed by `main.bicep`.

## Modules

| Module | Purpose |
|---|---|
| `identity.bicep` | User-assigned managed identities |
| `monitoring.bicep` | Log Analytics and workspace-based Application Insights |
| `monitoring-reader-sub.bicep` | Subscription-scoped Monitoring Reader assignment for the API |
| `network.bicep` / `privateendpoints.bicep` | Partial VNet/private-endpoint design scaffolding; not a served end-to-end isolation mode |
| `keyvault.bicep` | Key Vault and App Configuration RBAC; postprovision reconciles the label-aware warm sentinel with the deployment identity |
| `foundry.bicep` / `models.bicep` | Foundry accounts/projects and catalog-driven deployments |
| `data.bicep` | Cosmos (canonical state) plus document/media/blob containers; PostgreSQL was retired and deleted |
| `search.bicep` | Azure AI Search service and RBAC |
| `containerapps.bicep` | Container Apps environment and ACR |
| `api.bicep` / `web.bicep` | API and web Container Apps |
| `apimcore.bicep` / `gateway.bicep` | Shared APIM plus public SimpleL7Proxy HTTP/SSE edge, model/realtime APIs, policy, auth, and Foundry RBAC |
| `mcpgateway.bicep` / `apicenter.bicep` | Official MCP APIM plane and optional API Center inventory |
| `proxyasync.bicep` | Default-off AVM Blob + Service Bus backing for durable proxy async jobs |
| `eventhubs.bicep` | Default-off proxy metadata Event Hubs namespace/hub |
| `durabletask.bicep` | Optional Durable Task Scheduler and task hub |
| `alerts.bicep` / `cost.bicep` | Monitor alerting and budget tracking |

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

## Deployment prerequisites

Use the [greenfield standup guide](../docs/runbooks/greenfield-standup.md) for a
new subscription or tenant. It is the deployment procedure; this README is the
module map. A deployer needs:

- Azure CLI, Python 3.12, PowerShell 7, and azd 1.29.0;
- subscription-scope Contributor plus Role Based Access Control Administrator
  (or Owner), because the template creates the resource group and role assignments;
- registered resource providers and approved model/Marketplace access with quota
  in East US 2, Sweden Central, and the targeted West US catalog region;
- lowercase 3-20 character workload/environment tokens, at most 22 characters
  combined, plus a Foundry token that preserves the complete account-name suffix;
- deployment-owned Entra/OIDC, owner, cost-center, publisher, budget date, and
  alert-recipient values.

The checked-in showcase profile enables many paid features, but every such flag
is now an `AI4IA_*` azd binding. Review the
[configuration reference](../docs/configuration-reference.md) and opt out before
the first provision rather than editing `main.parameters.json`.

The azd `preprovision` hook runs catalog/feature validation and the live model
availability, lifecycle, and quota check before submitting ARM. Azure CLI must be
logged into the same `AZURE_SUBSCRIPTION_ID`; an absent or mismatched credential
fails precisely rather than allowing a partial paid/shared-resource deployment.
Lifecycle checks inventory the target Foundry deployments: an exact existing
`Succeeded` deployment warns and reconciles, while greenfield/absent/drifted records block.
Postprovision then hard-gates model state, gateway topology, App Configuration, and
enabled Content Understanding defaults.

The first release must use the repository deploy workflow after its GitHub
variables and OIDC trust are configured. Do not run standalone `azd provision`
against a healthy environment: Bicep carries placeholder images for greenfield
creation, while the release workflow captures the current revisions and promotes
the exact built digests afterward.

Validate in a separate subscription for full catalog fidelity, or use the
runbook's explicitly reduced validation profile when subscription-wide model
quota prevents a duplicate catalog; see
[`../docs/runbooks/teardown.md`](../docs/runbooks/teardown.md).

## Checking a deployed environment

Compare **resolved intent**, not raw defaults, with dated read-only evidence:

1. Record the source commit, target subscription/environment, and effective
   deployment overrides. A `maximum` capacity profile and an enabled Speech
   provider can legitimately differ from the portable defaults.
2. Use a subscription/resource-group-scoped Resource Graph inventory, checking
   pagination and total counts. Query child resources and important settings
   through their native ARM contracts; a successful top-level list is not proof
   that every child was returned.
3. Compare catalog-derived deployment names, `deployments[].version`, SKU,
   effective capacity, region, and upgrade policy. Exclude providers disabled
   by the resolved posture, not models that happen to be missing live.
4. Compare Container App runtime gates, image digests, scaling/ingress, storage
   schemas, authentication posture, and literal role-assignment scopes. Inspect
   secret references and credential scopes, never secret values.
5. Classify differences: approved override, release-owned image, provider-created
   supporting resource, retained legacy resource, or genuine drift. Do not
   automatically copy a live deviation into Bicep or delete it.

**APIM inventory trap:** the older Azure CLI API-list contract can omit native
MCP APIs. Use the module's `2024-06-01-preview` contract and the catalog-derived
`<server>-mcp` resource name, and verify product membership. An omitted row from
the older list is not evidence that the MCP route is absent.

A read-only observation on **2026-09-06**, against the deployment of commit
`95701acf`, found 34 Resource Graph rows and exact parity for 115 model deployments
from 48 enabled catalog entries (`maximum`, Claude off). The three Container Apps
ran registry digests; the shared Basic v2 APIM, optional Speech/MCP APIs, Cosmos
backup/vector posture, and Search configuration matched the resolved showcase.
See [feature posture](../docs/runbooks/feature-enablement.md#last-observed-deployment-posture).
This is configuration evidence, not a new deployment, a fresh exercise of every
data-plane feature, or a guarantee of future parity.

Later review corrections add explicit media gates, resource-region metrics
routing, credential stripping, and explicit OpenAPI false. Those contracts
require a normal approved provision/deploy; the dated observation is not a
claim that uncommitted or unreleased changes are already running.
