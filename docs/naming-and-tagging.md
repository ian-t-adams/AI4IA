# Naming & Tagging Conventions

## Resource naming
General Azure resources use `azd`-style names built from an abbreviation +
`<workload>-<environment>-<region>` plus a deterministic uniqueness suffix where required
(e.g. globally-unique Storage / ACR names). Abbreviations live in
[`infra/abbreviations.json`](../infra/abbreviations.json).

| Resource | Pattern | Example |
|---|---|---|
| Resource group | `rg-<workload>-<env>` | `rg-ai4ia-dev` |
| Foundry (Cognitive) account | `mf-<foundryToken>-<env>-<region>` | `mf-aiforia-dev-eastus2` |
| Foundry project | `proj-default-<foundryToken>-<env>-<region>` | `proj-default-aiforia-dev-eastus2` |
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
>
> **Two different tokens, both currently spelled `slurmfactory`.** They are independent and
> only coincide because the long-running environment is *named* after the subscription:
>
> - `naming.subscriptionToken` in [`infra/models.json`](../infra/models.json) is fixed, and
>   appears in **model deployment names** (`{model}-slurmfactory-{region}-{skuShort}`).
> - `<env>` is the azd environment name (`AZURE_ENV_NAME`), and appears in the **resource
>   group and Foundry account/project names**. In the current deployment it happens to be
>   `slurmfactory`, which is why that stack has `rg-ai4ia-slurmfactory` and
>   `mf-aiforia-slurmfactory-eastus2`.
>
> Changing the environment name therefore renames the resource group and the Foundry
> accounts but *not* the model deployments, and changing `subscriptionToken` does the
> reverse. A new tenant standing up an env called `prod` gets `rg-ai4ia-prod` and
> `mf-aiforia-prod-eastus2` while its deployments stay `{model}-slurmfactory-...` unless
> the token is also changed (see
> [deployment runbook §3](./runbooks/deployment.md)).

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
