# Deploying AI4IA with a coding agent

This guide is written **for an AI coding agent** — Claude Code, GitHub Copilot
CLI, or similar — driving an AI4IA deployment on a human's behalf. It states what
you may do autonomously, what you must stop and ask for, the exact commands, and
how to prove each step worked.

If you are a human reading this, you want
[Deploy to Azure](./runbooks/deploy-to-azure.md) and
[Greenfield Azure standup](./runbooks/greenfield-standup.md) instead. This page
deliberately omits the reasoning those runbooks carry and keeps only what an
agent needs to act correctly.

## Ground rules

AI4IA provisions paid Azure resources, creates tenant-wide identity objects, and
assigns subscription-scoped RBAC. Those are not reversible by re-running a
command.

**Do these autonomously.** Reading the repository, running the local app,
running tests and linters, running the read-only preflight scripts,
regenerating catalogs, and reporting findings.

**Stop and ask the human first.** Every one of these:

| Action | Why it needs a human |
| --- | --- |
| `azd provision`, `azd up`, or running the deploy workflow | Creates billed resources. APIM alone takes ~45 minutes to create and cannot be cheaply undone. |
| `az role assignment create` | Subscription-scoped RBAC. A wrong assignee id silently grants nothing (see [Traps](#traps-that-cost-real-time)). |
| `scripts/provision-entra-apps.ps1 -Apply` | Creates directory objects in the tenant. |
| Granting admin consent | Requires a privileged directory role and affects every user in the tenant. |
| Any DNS or custom-domain change | Affects live traffic and certificate issuance. |
| `scripts/teardown.ps1`, `scripts/purge-soft-deleted.ps1` | Destroys data. |
| Editing `infra/models.json` capacity upward | Can consume the subscription's entire model quota. |

**Never do these.** Do not commit secrets. Do not put a value the human gave you
into a file — repository variables and environment secrets are set through
`gh`, not the repo. Do not disable a failing preflight to get past it; the
preflight failing before ARM runs is the design.

State the assumption and continue when a choice is reversible. Ask when it is not.

## Phase 0 — Run it locally, no Azure

This works with no subscription and proves the checkout is sound. Two terminals.

```powershell
# Terminal 1 — API on :8080
cd app/api
python -m pip install -e ".[dev]"
Copy-Item .env.example .env       # defaults are AI4IA_ENV=local, dev auth
python -m uvicorn ai4ia_api.main:app --port 8080 --reload
```

```powershell
# Terminal 2 — web on :3000
cd app/web
npm ci
Copy-Item .env.example .env.local
npm run dev
```

Open `http://localhost:3000`. Dev auth needs no sign-in; the Next.js server-side
proxy injects `DEV_USER` as `X-Dev-User`. A browser-supplied `X-Dev-User` is
never trusted.

Without a model gateway, chat calls fail. That is expected — everything else
(sessions, navigation, settings, library UI) works. Local mode is for
development, not for demonstrating model behaviour.

Verify the checkout independently of any deployment:

```powershell
cd app/api;  python -m pytest -q; python -m ruff check .
cd app/web;  npm test; npm run lint
```

## Phase 1 — Preflight the subscription

Read-only. Run these before proposing anything that costs money, and report the
output verbatim.

```powershell
az login
az account set --subscription <subscription-id>
az account show --query "{tenant:tenantId,subscription:id,name:name}" -o table

python scripts/check-resource-providers.py
python scripts/check-model-availability.py
```

`check-resource-providers.py` derives the required namespaces from
`infra/**/*.bicep`, so it stays correct as the template changes. An untouched
subscription usually has most of them unregistered. Registering is additive and
free, but it is still a subscription-level change — ask, then:

```powershell
python scripts/check-resource-providers.py --register
```

Registration is asynchronous. Wait for every namespace to report `Registered`.

`check-model-availability.py` compares `infra/models.json` with the subscription
on three axes: **availability** (limited-access models need approval; partner
models can need a Marketplace offer), **lifecycle** (a `Deprecating` version can
keep serving an existing deployment while refusing a new one), and **quota**
(requested capacity summed by the scope Azure actually enforces).

If a model is unavailable or lacks quota, the fix is to request access/quota or
remove it from `infra/models.json` — never to skip the check. After any catalog
edit, regenerate and verify:

```powershell
python scripts/gen-model-catalog.py
python scripts/gen-gateway-policy.py
python scripts/validate-catalog.py
python scripts/gen-model-catalog.py --check
python scripts/gen-gateway-policy.py --check
```

The `--check` runs must exit 0. The same checks run as a non-optional `azd`
preprovision hook, so stale generated output fails the deployment.

## Phase 2 — Deployment identity and OIDC

Ask before running. Creates one user-assigned managed identity and two federated
credentials.

```powershell
$subscriptionId = az account show --query id -o tsv

az group create -n rg-ai4ia-cicd -l eastus2
az identity create -g rg-ai4ia-cicd -n id-ai4ia-deploy --location eastus2

$clientId    = az identity show -g rg-ai4ia-cicd -n id-ai4ia-deploy --query clientId -o tsv
$principalId = az identity show -g rg-ai4ia-cicd -n id-ai4ia-deploy --query principalId -o tsv
```

**Both** federated credentials are required on the same identity:

```powershell
# deploy.yml runs with `environment: production`, so its subject is environment-scoped.
az identity federated-credential create `
  --identity-name id-ai4ia-deploy -g rg-ai4ia-cicd `
  --name github-ai4ia-production `
  --issuer https://token.actions.githubusercontent.com `
  --subject "repo:<owner>/<repo>:environment:production" `
  --audiences api://AzureADTokenExchange

# pages.yml has no environment on its build job, so it uses the main-branch subject.
az identity federated-credential create `
  --identity-name id-ai4ia-deploy -g rg-ai4ia-cicd `
  --name github-ai4ia-main `
  --issuer https://token.actions.githubusercontent.com `
  --subject "repo:<owner>/<repo>:ref:refs/heads/main" `
  --audiences api://AzureADTokenExchange
```

Do not create a second Azure identity for Pages, and do not add an `environment:`
to the Pages build job — that changes its subject and breaks the trust.

RBAC (ask again — this is subscription-scoped):

```powershell
az role assignment create --assignee-object-id $principalId `
  --assignee-principal-type ServicePrincipal `
  --role Contributor --scope "/subscriptions/$subscriptionId"

az role assignment create --assignee-object-id $principalId `
  --assignee-principal-type ServicePrincipal `
  --role "Role Based Access Control Administrator" --scope "/subscriptions/$subscriptionId"
```

Both roles stay at subscription scope for routine provisioning:
`infra/main.bicep` has `targetScope = 'subscription'`, creates the resource
group, and submits subscription-scoped role assignments. Narrowing `Contributor`
to the resource group breaks every later `azd provision`.

Verify by scope, never by assignee — see [Traps](#traps-that-cost-real-time).

## Phase 3 — Configure GitHub

Set repository variables with `gh`. These are the ones a deploy refuses to run
without:

```powershell
gh variable set AI4IA_DEPLOYMENT_ENABLED --body true
gh variable set AZURE_CLIENT_ID           --body $clientId
gh variable set AZURE_TENANT_ID           --body <tenant-guid>
gh variable set AZURE_SUBSCRIPTION_ID     --body $subscriptionId
gh variable set AZURE_ENV_NAME            --body <3-20 lowercase chars>
gh variable set AZURE_LOCATION            --body eastus2
gh variable set AI4IA_APP_ENVIRONMENT     --body prod
gh variable set AI4IA_AUTH_PROVIDER       --body entra
gh variable set AI4IA_OWNER               --body <owner tag>
gh variable set AI4IA_COST_CENTER         --body <cost center tag>
gh variable set AI4IA_APIM_PUBLISHER_EMAIL --body <monitored mailbox>
gh variable set AI4IA_BUDGET_AMOUNT       --body <approved threshold>
gh variable set AI4IA_BUDGET_START_DATE   --body <yyyy-MM-01>
gh variable set AI4IA_ALERT_EMAIL         --body <deliverable mailbox>
```

`AI4IA_BUDGET_AMOUNT` is a **notification threshold, not a spending cap**. Say
that explicitly when you ask the human for a value.

`AI4IA_ALLOW_DEV_AUTH` must stay false. `AI4IA_MODEL_CAPACITY_PROFILE` must stay
`baseline` for a first deployment.

Feature flags default on for the showcase profile and are individually
overridable. Before proposing a narrower or cheaper environment, read
[the configuration reference](./configuration-reference.md) — a repository
variable does nothing unless `deploy.yml` exports it *and* the parameter file
consumes it, and `scripts/tests/test_configuration_reference_reachability.py`
enforces that mapping.

Then, in the GitHub UI (these are not reliably scriptable): restrict the
`production` environment's deployment branches to `main`, and set **Settings →
Pages → Source** to **GitHub Actions**.

## Phase 4 — Entra app registrations

Bicep cannot create directory objects. Two are required: an API registration and
a web SPA registration.

Inspect first — this pass makes no changes:

```powershell
./scripts/provision-entra-apps.ps1 -WebRedirectUri http://localhost:3000
```

Then ask, and apply:

```powershell
./scripts/provision-entra-apps.ps1 `
  -WebRedirectUri http://localhost:3000 `
  -AdminUpn <admin-upn> `
  -Apply
```

The script prints the `AI4IA_ENTRA_*` values. Set them as repository variables,
plus `AI4IA_ADMIN_SUBJECTS` (comma-separated Entra user object ids — admin access
is an API allowlist, not an Entra app role):

```powershell
az ad signed-in-user show --query id -o tsv    # the signed-in user's oid
```

The script is idempotent and retains existing redirect URIs, so it is rerun in
Phase 5 to add the deployed origin. That rerun is required even when no custom
domain will ever be used.

## Phase 5 — First provision

The first standup **must** go through the GitHub workflow, not a local
`azd up`. The workflow resolves the deployment identity's principal id, exports
it as `AZURE_PRINCIPAL_ID`, and Bicep grants that service principal the narrow
roles the postprovision hook needs. A local operator is neither the API managed
identity nor that service principal, and the role-assignment template declares
`principalType: 'ServicePrincipal'`, so substituting a user object id is not a
supported workaround.

Leave all four custom-domain variables empty for this run
(`AI4IA_WEB_CUSTOM_DOMAIN`, `AI4IA_WEB_MANAGED_CERT_NAME`,
`AI4IA_PROXY_CUSTOM_DOMAIN`, `AI4IA_PROXY_MANAGED_CERT_NAME`).

Ask, then:

```powershell
gh workflow run deploy.yml -f provision=true --ref main
gh run watch
```

Expect roughly an hour on a greenfield subscription; APIM dominates. There are no
prior revisions on a first run, so a failure must be corrected and rerun rather
than rolled back.

When it succeeds, record the hostnames and rerun the Entra script to add the real
web origin:

```powershell
$web = az containerapp show -g rg-ai4ia-<env> -n ca-web-<env> `
  --query "{fqdn:properties.configuration.ingress.fqdn,verificationId:properties.customDomainVerificationId}" |
  ConvertFrom-Json

./scripts/provision-entra-apps.ps1 `
  -WebRedirectUri "https://$($web.fqdn)",http://localhost:3000 `
  -AdminUpn <admin-upn> `
  -Apply
```

Confirm browser sign-in at `https://$($web.fqdn)` before touching DNS.

## Phase 6 — Data plane and validation

Bicep creates the Foundry control-plane wiring but not the data-plane toolbox.

```powershell
python -m pip install -e "app/api[foundry]"    # provisioning-only extra

$projectEndpoint = azd env get-value AZURE_FOUNDRY_PROJECT_ENDPOINT
if (-not $projectEndpoint) { throw "AZURE_FOUNDRY_PROJECT_ENDPOINT is empty" }
$env:AZURE_FOUNDRY_PROJECT_ENDPOINT = $projectEndpoint

python scripts/provision-foundry-toolbox.py --check-access   # diagnostic only
gh workflow run foundry-assets.yml --ref main -f project_endpoint=$projectEndpoint
```

Derive the endpoint from `azd`; never construct it by hand. The workflow is
authoritative — the local `--check-access` only tells you whether *your* identity
has project-scoped Foundry User.

## Routine deployments after the first

Subsequent releases promote images by digest rather than rebuilding during
deploy:

```powershell
gh workflow run deploy.yml --ref main                    # code only
gh workflow run deploy.yml -f provision=true --ref main  # infra changed
```

A standalone `azd provision` is **not** the release path for an existing
environment: the Bicep app modules carry placeholder images for greenfield
creation, so provisioning alone can push a placeholder revision.
See [the routine deployment runbook](./runbooks/deployment.md).

## Verification

A green workflow is not proof the app works. Assert these:

```powershell
# 1. Running revisions carry the digests this run pushed (the job summary lists them).
az containerapp show -g rg-ai4ia-<env> -n ca-api-<env> `
  --query "properties.template.containers[].image" -o tsv

# 2. Every model deployment succeeded.
az cognitiveservices account deployment list -g rg-ai4ia-<env> -n <foundry-account> `
  --query "[].{name:name,state:properties.provisioningState}" -o table

# 3. Custom domain bindings are SniEnabled, not just present.
az containerapp show -g rg-ai4ia-<env> -n ca-web-<env> `
  --query "properties.configuration.ingress.customDomains[].{name:name,binding:bindingType}"
```

Then have the human sign in and send one chat message. Model routing depends on
APIM policy, the proxy, Foundry RBAC, and the catalog agreeing — nothing short of
a real turn proves all four.

## Traps that cost real time

These are the failures most likely to catch an agent. Each has been hit here.

**`--assignee-object-id` takes the principal id, not the client id.** A managed
identity has both. `az role assignment create --assignee-object-id <clientId>
--assignee-principal-type ServicePrincipal` **succeeds** and grants nothing,
because `--assignee-principal-type` skips directory validation. Read the
principal id from the resource (`az identity list -g <rg> --query
"[].{n:name,principalId:principalId}"`), never from a role listing —
`az role assignment list` prints the *clientId* in `principalName` for managed
identities, which is exactly how the wrong value gets copied. Verify with
`--scope <resource>`, not `--assignee`: the assignee form resolves the id first
and will happily report roles that are attached to a different object.

**Custom domains need two passes.** Azure requires the hostname to exist in the
Container Apps environment before it issues a managed certificate, but Bicep
declares the certificate and the ingress binding together and ARM creates the
certificate first. A single pass fails with
`RequireCustomHostnameInEnvironment`. Provision with the domain variables empty,
add DNS `CNAME` + `TXT asuid.<host>`, then set the variables and provision again.
The workflow runs `az containerapp hostname add` as a preflight; a local
provision must do that step manually.

**Never copy certificate names between tenants.** Managed certificates belong to
one Container Apps environment. Once issued, record the actual names in
`AI4IA_WEB_MANAGED_CERT_NAME` / `AI4IA_PROXY_MANAGED_CERT_NAME`. Every later
provision must pass all four domain values — omitting a hostname removes its
binding.

**Do not hardcode a model deployment name.** `infra/models.json` is the source of
truth and generates the packaged runtime catalog. Names carry subscription
tokens, so a literal that works in one environment is wrong in the next.

**Do not add a direct Foundry call.** HTTP/SSE model traffic goes
SimpleL7Proxy → APIM → Foundry; realtime WebSockets go FastAPI relay → APIM →
Foundry because SimpleL7Proxy has no WebSocket support. The only direct-Foundry
exception is the Responses-API Code Interpreter, whose stateful sandbox is not a
routable catalog deployment. Anything else is a security architecture change.

**A rollback restores container revisions only.** Nothing reverts what
`azd provision` changed — APIM policy and fragments, named values, model
deployments, RBAC. A bad generated gateway policy survives rollback and re-fails
every later deploy. Treat a policy change as higher risk than a code change.

## When you get stuck

`docs/runbooks/deployment.md` has a troubleshooting section keyed by the exact
error string Azure returns — `ServiceModelDeprecating`, `InsufficientQuota`,
`429 No Backends Available`, `400 model_path_mismatch`,
`RequireCustomHostnameInEnvironment`, `DeploymentActive`, `ServiceAlreadyExists`,
and others. Search that file for the literal message before improvising.

Report the failure and what you have verified. Do not retry a provision more than
once without a changed input: repeated provisions of an unchanged bad template
create `DeploymentActive` conflicts that then need their own cleanup.
