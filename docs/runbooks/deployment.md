# Runbook: Deployment (CI/CD to Azure)

> How AI4IA gets from a merged commit on `main` to a running app in Azure, and the **one-time**
> setup that makes merges deploy automatically.
>
> Source of truth: `.github/workflows/deploy.yml`, `azure.yaml` (azd service map),
> `infra/main.bicep` + `infra/main.parameters.json`.

## TL;DR — does merging to `main` redeploy?

**Only after the one-time OIDC setup in [section 2](#2-one-time-setup-make-merges-deploy) is done.**

Before that setup, the repo has **no continuous deployment** — `app-ci.yml` and `infra-validate.yml`
only *validate* (lint / build / test / Bicep build). They never call `azd`. So `main` can be many
commits ahead of what is actually running, and the only way to ship was a manual `azd up` from a
workstation. If the deployed app looks stale, that is why.

`deploy.yml` closes the gap: once configured, every push to `main` that touches `app/**`, `infra/**`,
`proxy/**`, or `azure.yaml` provisions infra and deploys the new images. Until `AZURE_CLIENT_ID` is
set, the job is a deliberate **no-op** (so the workflow is safe to merge before the identity exists).

## 1. How a deploy runs

```
push to main (app/infra/proxy/azure.yaml)        manual: Actions -> deploy -> Run workflow
                 │                                              │
                 └──────────────────────┬───────────────────────┘
                                        ▼
                         deploy.yml  (environment: production)
                                        ▼
              azd auth login  (GitHub OIDC federated credential — no secrets)
                                        ▼
              azd provision --no-prompt   (Bicep: infra/main.bicep, idempotent)
                                        ▼
              azd deploy --no-prompt      (build + push web / api / proxy images)
```

- **Concurrency:** runs are serialized on the `deploy-production` group and **not** cancelled
  mid-flight, so an in-progress provision/deploy always finishes cleanly.
- **Provision on every app-only change** is intentional: `azd provision` is idempotent and keeps
  infra reconciled. A manual run can skip it (`Run workflow` → uncheck *provision*).
- **Path filter:** doc-only merges (e.g. `docs/**`) do **not** trigger a deploy. Use the manual
  `workflow_dispatch` trigger to force one.

## 2. One-time setup: make merges deploy

No secrets live in the repo. azd authenticates with a **GitHub OIDC federated credential**, so there
is no client secret to rotate.

### 2.1 Create a deployment identity with a federated credential

Use a user-assigned managed identity (or an app registration). The federated credential trusts
tokens GitHub Actions issues for this repo's `production` environment.

```powershell
az login
$sub = az account show --query id -o tsv

# 1) user-assigned managed identity
az identity create -g rg-ai4ia-cicd -n id-ai4ia-deploy --location eastus2
$clientId    = az identity show -g rg-ai4ia-cicd -n id-ai4ia-deploy --query clientId -o tsv
$principalId = az identity show -g rg-ai4ia-cicd -n id-ai4ia-deploy --query principalId -o tsv

# 2) federated credential for the 'production' environment on this repo
az identity federated-credential create `
  --identity-name id-ai4ia-deploy -g rg-ai4ia-cicd `
  --name github-ai4ia-production `
  --issuer https://token.actions.githubusercontent.com `
  --subject "repo:ian-t-adams/AI4IA:environment:production" `
  --audiences api://AzureADTokenExchange
```

> Add a second federated credential with subject `repo:ian-t-adams/AI4IA:ref:refs/heads/main`
> if you also want pushes (not just the `production` environment) to authenticate.

### 2.2 Grant the identity the roles azd needs

azd provisions a full resource group (Container Apps, ACR, Cosmos, Key Vault, APIM, etc.) and pushes
container images. At minimum:

```powershell
# Provision + manage resources in the subscription (scope down to the RG once it exists if preferred)
az role assignment create --assignee $principalId --role "Contributor"           --scope "/subscriptions/$sub"
# Assign roles in Bicep (the template grants managed identities their data-plane roles)
az role assignment create --assignee $principalId --role "Role Based Access Control Administrator" --scope "/subscriptions/$sub"
# Push images to the provisioned ACR (AcrPush is also granted in-template; this covers first run)
```

> Prefer least privilege: after the first successful `azd provision`, narrow `Contributor` to the
> created resource group scope. `Role Based Access Control Administrator` is needed because the Bicep
> assigns data-plane roles (e.g. Cosmos, Storage, Key Vault) to the app's managed identities.

### 2.3 Add the repository variables

Settings → Secrets and variables → Actions → **Variables** (these are identifiers, not secrets):

| Variable | Value |
|---|---|
| `AZURE_CLIENT_ID` | client ID of `id-ai4ia-deploy` |
| `AZURE_TENANT_ID` | your Entra tenant ID |
| `AZURE_SUBSCRIPTION_ID` | target subscription ID |
| `AZURE_ENV_NAME` | azd environment name, e.g. `ai4ia-dev` |
| `AZURE_LOCATION` | primary region, e.g. `eastus2` |
| `AI4IA_OWNER` | accountable owner tag value for the deployed resources |
| `AI4IA_APIM_PUBLISHER_EMAIL` | operator-owned APIM publisher mailbox |
| `AI4IA_BUDGET_START_DATE` | *(optional, recommended)* fixed budget start month `yyyy-MM-01`. Empty defaults to the first of the current month, which **drifts and breaks the first deploy of each new month** (see §7.2). Pin it to keep redeploys idempotent. |

The moment `AZURE_CLIENT_ID` is set, the next qualifying push to `main` deploys.

### 2.4 (Recommended) protect the `production` environment

Settings → Environments → `production` → add **Required reviewers** so a human approves each deploy,
and/or restrict to the `main` branch. Until you add protection, the environment exists with no gate.

### 2.5 Custom domains (vanity hostnames) — **required if you use them**

Pre-deploy checklist:

1. Confirm the DNS `CNAME` and `asuid.<host>` `TXT` records already exist at the
   DNS provider.
2. Confirm the target Container Apps managed certificate names if adopting
   existing certs.
3. Set all four custom-domain repository variables before `azd provision`.
4. Treat missing variables as an outage risk, not a harmless omission.

The app is reached at vanity hostnames (`ai4ia.nomad-analytics.com` for the web app,
`genaiproxy.nomad-analytics.com` for the proxy). The binding + Azure-managed TLS cert are declared in
Bicep (`infra/modules/web.bicep`, `infra/modules/gateway.bicep`) but only when these repo variables
are set. **If they are empty, `azd provision` resets the ingress to *no* custom domain** and the live
site fails with `ERR_CONNECTION_CLOSED` on the vanity hostname (the default
`*.azurecontainerapps.io` FQDN keeps working). DNS records are untouched — only the Azure-side
binding is dropped.

| Variable | Value (this deployment) |
|---|---|
| `AI4IA_WEB_CUSTOM_DOMAIN` | `ai4ia.nomad-analytics.com` |
| `AI4IA_WEB_MANAGED_CERT_NAME` | `mc-cae-ai4ia-slur-ai4ia-nomad-anal-2891` |
| `AI4IA_PROXY_CUSTOM_DOMAIN` | `genaiproxy.nomad-analytics.com` |
| `AI4IA_PROXY_MANAGED_CERT_NAME` | `mc-cae-ai4ia-slur-genaiproxy-nomad-6552` |

The `*_MANAGED_CERT_NAME` values **adopt the existing managed cert** (look it up with
`az containerapp env certificate list -n <managed-env> -g <rg> --managed-certificates-only`) so the
deploy reuses the issued cert instead of creating a duplicate subject. DNS prerequisites must exist at
your DNS provider: a `CNAME` from the vanity host to the app's `*.azurecontainerapps.io` FQDN, and an
`asuid.<host>` `TXT` record holding the domain-verification ID
(`az containerapp show ... --query properties.customDomainVerificationId`, or read it off the existing
managed cert). These records are external to Azure, so a reprovision never touches them.

If a deploy ever wipes the binding before these vars were set, rebind imperatively to restore service
immediately (cert survives on the environment), then ensure the vars are set so it stays bound:

```powershell
az containerapp hostname bind --hostname ai4ia.nomad-analytics.com `
  -g <rg> -n ca-web-<env> --environment <managed-env> `
  --certificate mc-cae-ai4ia-slur-ai4ia-nomad-anal-2891
```

### 2.6 Web IQ API key (secret)

Web search is enabled in `infra/main.parameters.json`, and `webIqApiKey` reads from the
`AI4IA_WEBIQ_API_KEY` environment variable. Unlike the identifiers in §2.3, this is a **secret**, so
it lives as a **`production` environment secret** and is mapped into the deploy job in
`.github/workflows/deploy.yml` (`AI4IA_WEBIQ_API_KEY: ${{ secrets.AI4IA_WEBIQ_API_KEY }}`).

Set it once (or via Settings → Environments → `production` → **Secrets**):

```powershell
gh secret set AI4IA_WEBIQ_API_KEY --env production
```

Like the custom-domain variables in §2.5, an **empty value at provision time is not harmless**: bicep
computes `hasWebIqKey = false`, drops the `webiq-api-key` Container App secret, and falls back to
authenticating Web IQ with the api's managed identity. That identity must be **entitled to Web IQ**
(the `https://api.microsoft.ai/.default` scope) or every live-search call returns HTTP 401. So either
keep this secret set, **or** entitle the managed identity and leave it empty on purpose.

> Rotating the key is one update to this secret plus a deploy (the next `azd provision` re-writes the
> Container App secret). Diagnose live-search auth state from the admin **Web search health** panel,
> which reports `authMode` (`api_key` / `managed_identity` / `unconfigured`) and categorized failures.

## 3. Moving to a new subscription or tenant (1:1 standup)

The stack is data-driven, so standing it up in a **new subscription/tenant** is a small set of
config edits plus the normal deploy — no code changes. What varies per environment is centralized:

| What | Where | Notes |
|---|---|---|
| Environment name | `AZURE_ENV_NAME` repo/azd var | Feeds `environmentName`; names the RG (`rg-ai4ia-<env>`), Foundry accounts/projects (`mf-aiforia-<env>-<region>`), Container Apps, etc. |
| Subscription / tenant / region | `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`, `AZURE_LOCATION` repo vars | See §2.3. |
| Model deployment-name token | `infra/models.json` → `naming.subscriptionToken` | Stamped into every model deployment name (`{model}-<token>-<region>-<sku>`). Read by bicep **and** the runtime catalog. |
| Foundry account/project token | `infra/models.json` → `naming.foundryToken` | Names `mf-<token>-<env>-<region>` and the toolbox project endpoint. |
| Postgres region | `AI4IA_POSTGRES_LOCATION` | Must be unrestricted for the subscription (see §7.1). |
| API Center region | `AI4IA_API_CENTER_LOCATION` | Only if `enablePrivateToolCatalog=true`; not available in every region (see §7.2 / the API Center note). |
| Custom domains | `AI4IA_*_CUSTOM_DOMAIN` / `*_MANAGED_CERT_NAME` | Leave empty for a vanilla hostname; see §2.5. |

Procedure:

1. Set the repo variables for the new subscription/tenant/env (§2.3), plus `AI4IA_POSTGRES_LOCATION`
   and (if used) `AI4IA_API_CENTER_LOCATION` to regions valid there.
2. If you want a different naming token, edit `infra/models.json` `naming.subscriptionToken` and
   `naming.foundryToken`, then **regenerate the runtime catalog** so routing matches the deployments:

   ```powershell
   python scripts/gen-model-catalog.py     # rewrites app/api/src/ai4ia_api/data/model_catalog.json
   python scripts/gen-model-catalog.py --check   # CI drift guard; must pass
   python scripts/validate-catalog.py            # names/regions/SKUs consistent
   ```

   The tokens are the single source of truth (bicep, the generator, the validator, and the app all
   read `infra/models.json` `naming`), so there is nothing else to change for naming to stay 1:1.
3. `azd up`. Model deployments, Foundry accounts/projects, and the whole stack come up under the new
   names.
4. **Foundry toolbox (data-plane, if used):** the toolbox is not created by `azd up`. After the
   deploy, run `python scripts/provision-foundry-toolbox.py --create` against the new project (the
   `infra/mcp-servers.json` entry is already portable — its APIM upstream URL is computed by bicep
   from the new project endpoint). See [`../foundry-toolbox.md`](../foundry-toolbox.md).
5. **Break-glass ops scripts** (`scripts/inventory.ps1`, `teardown.ps1`, `purge-soft-deleted.ps1`)
   default their `-ResourceGroup` / `-NameFilter` to the original environment on purpose (so they
   cannot accidentally target the wrong stack). Pass explicit arguments for the new environment.

## 4. Enabling feature-flagged capabilities at deploy time

Feature flags (Voice Live, document library, memory, etc.) are **not** turned on by this pipeline —
they are azd environment values consumed by `infra/main.parameters.json`. To enable one, set the azd
env var (locally `azd env set <NAME> <value>`, or as an environment variable available to the
workflow) and let the next provision apply it. The exact flags, required resources, and fail-closed
prerequisites for each feature are in [`feature-enablement.md`](./feature-enablement.md).

## 5. Manual deploy (no pipeline / break-glass)

From a workstation with the azd env selected:

```powershell
az login
azd env select ai4ia-dev
azd up            # provision + deploy in one step
# or, more granular:
azd provision
azd deploy
```

> Validate in a parallel resource group before reprovisioning a live stack — see
> [`teardown.md`](./teardown.md).

## 6. Rollback

There is no automatic rollback. To revert, deploy a known-good commit:

```powershell
# re-run the deploy workflow from a previous green commit, or locally:
git checkout <good-sha>
azd deploy
```

Container Apps keeps prior revisions; you can also shift traffic back to a previous revision in the
portal/CLI while a fix is prepared.

## 7. Troubleshooting

### 7.1 `Provision infrastructure` fails with `LocationIsOfferRestricted` (Postgres)

Symptom — the deploy job fails in **Provision infrastructure** with:

```
(x) Failed: Azure Database for PostgreSQL flexible server: psql-...
LocationIsOfferRestricted: Subscriptions are restricted from provisioning in location '<region>'.
```

Cause — the Postgres Flexible Server (mem0/pgvector home) is being provisioned in a region
where **this subscription is offer-restricted** for that resource. It is a subscription-level
policy, not a quota/capacity issue, and it surfaces only at provision time — `az bicep build` and the
other resources (Cosmos, Container Apps, Foundry) succeed in the same region. The `slurmfactory`
subscription is restricted in `eastus2`, `eastus`, and `westus2`.

Fix — point `postgresLocation` at an **unrestricted** region. The default is `centralus`
(`infra/main.parameters.json` → `AI4IA_POSTGRES_LOCATION`). The server name embeds its region, so
changing it yields a fresh `resourceId` (no ARM location-immutability conflict with a prior attempt).
Verify a candidate region before switching:

```powershell
$sub = (az account show --query id -o tsv)
az rest --method get --url "https://management.azure.com/subscriptions/$sub/providers/Microsoft.DBforPostgreSQL/locations/<region>/capabilities?api-version=2024-08-01" --query "value[0].{restricted:restricted, reason:reason}" -o json
# restricted: "Disabled" (with reason: null) means the region is usable.
```


### 7.2 `Provision infrastructure` fails with `Start date of budgets cannot be updated`

Symptom — the deploy job fails in **Provision infrastructure**, after every other resource
succeeds, with:

```
400: Start date of budgets cannot be updated. Please delete and create a new budget.
```

Cause — the resource-group budget (`infra/modules/cost.bicep`) needs a start date that is the
first of a month. When `AI4IA_BUDGET_START_DATE` is unset, bicep defaults it to the first of the
*current* month (`utcNow`). Azure forbids changing an existing budget's start date, so the first
deploy of each new month tries to move the start date forward and is rejected. It is unrelated to
any application or infra change in the triggering commit.

Fix — pin the start date so it never drifts, then reconcile the existing budget once. Pick one:

- **Match the existing budget (no deletion).** In the portal open Cost Management → Budgets →
  `budget-ai4ia-<env>` and read its start date. Set the `AI4IA_BUDGET_START_DATE` repo variable to
  that exact `yyyy-MM-01`. The next deploy sends the unchanged value, so there is no update to
  reject, and it stays idempotent forever.
- **Delete and recreate.** Delete `budget-ai4ia-<env>` in the target subscription (Azure's own
  suggestion), set `AI4IA_BUDGET_START_DATE` to the first of the current month (`yyyy-MM-01`), then
  re-run the deploy. It is recreated with the pinned date and stays idempotent.

Set the variable with the CLI:

```powershell
gh variable set AI4IA_BUDGET_START_DATE --body "2026-07-01"
```

The value is a global repo variable, so use the first of the month in which the budget is (re)created.
A brand-new environment created in a later month should use that later month (a monthly budget's start
date cannot be more than the current period in the past).
