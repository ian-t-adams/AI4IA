# Naming & Tagging Conventions

## Resource naming
General Azure resources use `azd`-style names built from an abbreviation +
`<workload>-<environment>` (plus `-<region>` where the resource is per-region).
Abbreviations live in [`infra/abbreviations.json`](../infra/abbreviations.json).

**Every resource whose name must be globally unique across Azure also carries
`<suffix>`** — `uniqueString(subscription().id, environmentName)`, computed once in
[`infra/main.bicep`](../infra/main.bicep). This is load-bearing, not cosmetic: names that
back public DNS (`<apim>.azure-api.net`, `<account>.services.ai.azure.com`,
`<vault>.vault.azure.net`, …) are reserved tenant-wide *and* subscription-wide, so an
unsuffixed name can only ever deploy into the one subscription that already owns it.
Omitting it is exactly what broke the first cutover attempt — see
[deployment runbook §7.6](./runbooks/deployment.md). `scripts/tests/test_bicep_naming.py`
(run by `infra-validate`) fails the build if a globally-unique resource loses its suffix.

| Resource | Globally unique? | Pattern | Example |
|---|---|---|---|
| Resource group | no | `rg-<workload>-<env>` | `rg-ai4ia-dev` |
| Foundry (Cognitive) account | **yes** | `mf-<foundryToken>-<env>-<region>-<suffix>` | `mf-aiforia-dev-eastus2-3k7x` |
| Foundry project | no (child) | `proj-default-<foundryToken>-<env>-<region>` | `proj-default-aiforia-dev-eastus2` |
| Container Apps env | no | `cae-<workload>-<env>`, `-vnet` when VNet-injected | `cae-ai4ia-dev` |
| Container App | no | `ca-<service>-<env>` | `ca-api-dev`, `ca-web-dev`, `ca-proxy-dev` |
| Key Vault | **yes** | `kv<workload><suffix>` (≤24) | `kvai4ia3k7x` |
| Cosmos DB | **yes** | `cosmos-<workload>-<env>-<suffix>` (≤44) | `cosmos-ai4ia-dev-3k7x` |
| Container Registry | **yes** | `acr<workload><suffix>` | `acrai4ia3k7x` |
| AI Search | **yes** | `srch-<workload>-<suffix>` | `srch-ai4ia-3k7x` |
| Storage | **yes** | `st<uniqueString(rg)>` / `sti<uniqueString(rg)>` | `stabc123…` |
| APIM | **yes** | `apim-mcp-<workload>-<env>-<suffix>` (≤50) | `apim-mcp-ai4ia-dev-3k7x` |
| API Center | **yes** | `apic-<workload>-<env>-<suffix>` | `apic-ai4ia-dev-3k7x` |
| Event Hubs namespace (when proxy telemetry is enabled) | **yes** | `evhns-<workload>-<env>-<suffix>` | `evhns-ai4ia-dev-3k7x` |
| App Configuration | **yes** | `appcs-<workload>-<env>-<suffix>` | `appcs-ai4ia-dev-3k7x` |
| Log Analytics | no | `log-<workload>-<env>` | `log-ai4ia-dev` |
| App Insights | no | `appi-<workload>-<env>` | `appi-ai4ia-dev` |
| User-assigned identity | no | `id-<service>-<env>` | `id-api-dev` |

> **Why the Foundry *project* is deliberately unsuffixed.** It is a child of the account,
> so it inherits the account's uniqueness and needs none of its own — and the account name
> is already 51 characters against Cognitive Services' 60-character cap, so suffixing the
> project (61) would silently truncate. Pinned by `test_foundry_project_is_not_suffixed`.

> The existing brand prefix `aiforia` (= **AI4IA**) and the `slurmfactory` token are kept for
> Foundry/model resources to match operator expectations. New app-tier resources use the
> shorter `ai4ia` workload token.
>
> **Two different tokens, both currently spelled `slurmfactory`.** They are independent and
> only coincide because the long-running environment is *named* after the subscription:
>
> - `naming.subscriptionToken` in [`infra/models.json`](../infra/models.json) is fixed, and
>   appears in **model deployment names** (`{model}-slurmfactory-{region}-{skuShort}`).
> - `<env>` is the azd environment name (`AZURE_ENV_NAME`), and appears in the **resource
>   group and Foundry account/project names**. In the current deployment it happens to be
>   `slurmfactory`, which is why that stack has `rg-ai4ia-slurmfactory` and
>   `mf-aiforia-slurmfactory-eastus2-<suffix>`.
>
> Changing the environment name therefore renames the resource group and the Foundry
> accounts but *not* the model deployments, and changing `subscriptionToken` does the
> reverse. A new tenant standing up an env called `prod` gets `rg-ai4ia-prod` and
> `mf-aiforia-prod-eastus2-<suffix>` while its deployments stay `{model}-slurmfactory-...`
> unless the token is also changed (see
> [greenfield standup §5](./runbooks/greenfield-standup.md#5-select-environment-posture-and-names)).

## Model deployment naming
Deployments follow `infra/models.json` `naming.pattern`:

```
{model}-slurmfactory-{region}-{skuShort}
```
- `skuShort`: `GlobalStandard` → `glbl`, `DataZoneStandard` → `dz`, `Standard` → `std`.
- Example: `gpt-5.2-slurmfactory-eastus2-glbl`, `gpt-image-2-slurmfactory-swedencentral-glbl`.

## Required tags
Every resource group and resource carries these tags (applied via Bicep):

| Tag | Purpose | Example |
|---|---|---|
| `workload` | App identifier | `ai4ia` |
| `env` | Environment | `dev` / `demo` / `prod` |
| `azd-env-name` | azd environment binding | `ai4ia-dev` |
| `costCenter` | Chargeback | `genai-demo` |
| `owner` | Accountable owner | `ai4ia-operator` or the owning team/person |
| `dataZone` | Data residency of the resource | `US` / `EU` |
| `managedBy` | Provisioning system | `azd-bicep` |

## Environments
`dev` (default working env), `demo` (customer-facing showcase), `prod` (hardened). Each maps to
its own `azd` environment + resource group; `infra/main.parameters.json` reads `AZURE_ENV_NAME`.
