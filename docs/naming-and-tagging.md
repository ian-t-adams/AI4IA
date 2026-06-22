# Naming & Tagging Conventions

## Resource naming
General Azure resources use `azd`-style names built from an abbreviation +
`<workload>-<environment>-<region>` plus a deterministic uniqueness suffix where required
(e.g. globally-unique Storage / ACR names). Abbreviations live in
[`infra/abbreviations.json`](../infra/abbreviations.json).

| Resource | Pattern | Example |
|---|---|---|
| Resource group | `rg-<workload>-<env>` | `rg-ai4ia-dev` |
| Foundry (Cognitive) account | `mf-aiforia-slurmfactory-<region>` | `mf-aiforia-slurmfactory-eastus2` |
| Foundry project | `proj-default-aiforia-slurmfactory-<region>` | `proj-default-aiforia-slurmfactory-eastus2` |
| Container Apps env | `cae-<workload>-<env>` | `cae-ai4ia-dev` |
| Container App | `ca-<service>-<env>` | `ca-api-dev`, `ca-web-dev`, `ca-proxy-dev` |
| Key Vault | `kv-<workload><env><suffix>` | `kvai4iadev3k7x` |
| Cosmos DB | `cosmos-<workload>-<env>` | `cosmos-ai4ia-dev` |
| Postgres Flexible | `psql-<workload>-<env>` | `psql-ai4ia-dev` |
| Container Registry | `cr<workload><env><suffix>` | `crai4iadev3k7x` |
| APIM | `apim-<workload>-<env>` | `apim-ai4ia-dev` |
| Event Hubs namespace | `evhns-<workload>-<env>` | `evhns-ai4ia-dev` |
| Log Analytics | `log-<workload>-<env>` | `log-ai4ia-dev` |
| App Insights | `appi-<workload>-<env>` | `appi-ai4ia-dev` |
| App Configuration | `appcs-<workload>-<env>` | `appcs-ai4ia-dev` |
| User-assigned identity | `id-<service>-<env>` | `id-api-dev` |

> The existing brand prefix `aiforia` (= **AI4IA**) and the `slurmfactory` token are kept for
> Foundry/model resources to match operator expectations. New app-tier resources use the
> shorter `ai4ia` workload token.

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
