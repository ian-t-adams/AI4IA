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

The moment `AZURE_CLIENT_ID` is set, the next qualifying push to `main` deploys.

### 2.4 (Recommended) protect the `production` environment

Settings → Environments → `production` → add **Required reviewers** so a human approves each deploy,
and/or restrict to the `main` branch. Until you add protection, the environment exists with no gate.

## 3. Enabling feature-flagged capabilities at deploy time

Feature flags (Voice Live, document library, memory, etc.) are **not** turned on by this pipeline —
they are azd environment values consumed by `infra/main.parameters.json`. To enable one, set the azd
env var (locally `azd env set <NAME> <value>`, or as an environment variable available to the
workflow) and let the next provision apply it. The exact flags, required resources, and fail-closed
prerequisites for each feature are in [`feature-enablement.md`](./feature-enablement.md).

## 4. Manual deploy (no pipeline / break-glass)

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

## 5. Rollback

There is no automatic rollback. To revert, deploy a known-good commit:

```powershell
# re-run the deploy workflow from a previous green commit, or locally:
git checkout <good-sha>
azd deploy
```

Container Apps keeps prior revisions; you can also shift traffic back to a previous revision in the
portal/CLI while a fix is prepared.
