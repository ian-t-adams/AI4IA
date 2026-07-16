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
| `AI4IA_PROXY_WORKERS` / `AI4IA_PROXY_MIN_REPLICAS` / `AI4IA_PROXY_MAX_REPLICAS` | Optional proxy capacity overrides. Minimum defaults to `1`; do not scale the active gateway to zero. |
| `AI4IA_PROXY_PRIORITIES_ENABLED` / `AI4IA_PROXY_PRIORITY_WORKERS` | Optional, default off. Worker reservations such as `1:2,3:1`; fairness remains per replica. |
| `AI4IA_PROXY_EVENTHUB_TELEMETRY_ENABLED` | Optional, default off metadata telemetry. |
| `AI4IA_PROXY_ASYNC_ENABLED` | Optional, default off dedicated Blob + Service Bus durable async plane. |
| `AI4IA_PROXY_PROFILES_ENABLED` / `AI4IA_PROXY_PROFILE_PROJECTION_JSON` | Keep disabled until Entra workload identity is wired at the proxy edge; validation intentionally fails otherwise. The JSON value is a secret. |

The moment `AZURE_CLIENT_ID` is set, the next qualifying push to `main` deploys.

The model gateway has no Front Door in this phase. Point model clients at the
proxy custom/default FQDN. DNS/custom domain terminates on the proxy Container
App, which calls APIM; APIM alone has Foundry model RBAC.

Both `azd provision` and the deploy workflow run the model/MCP/gateway drift
checks plus `validate-feature-prereqs.py` before provisioning. The validator
resolves the actual `AI4IA_*` environment values, not only the defaults embedded
in `main.parameters.json`.

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

After provisioning gateway changes, verify direction before application smoke
tests:

1. `AZURE_MODEL_GATEWAY_URL` ends in the proxy `/openai` URL.
2. `AZURE_APIM_GATEWAY_URL` is different and is not configured as an application
   model URL.
3. APIM's `openai` API service URL terminates at Foundry, never at `ca-proxy`.
4. The proxy `Host1` terminates at APIM and its subscription key is an ACA secret.
5. Voice Live uses `AZURE_REALTIME_GATEWAY_URL` through the FastAPI relay for
   `azure_openai`, and, when enabled, a second distinct
   `AI4IA_SPEECH_VOICE_LIVE_BASE_URL`/key pair for `speech_voice_live` — never the
   proxy.
6. `AZURE_PROXY_APP_NAME` names the Container App used for revision-level
   inspection and rollback.

Do not add a second retry loop around model writes. APIM performs bounded
same-dispatch regional failover; SimpleL7Proxy owns delayed requeue from the
`S7PREQUEUE` contract.

The `postprovision` hook hard-gates these output relationships even though it runs
before application image deployment.

#### APIM policy compiler preflight

ARM what-if validates resource changes, not APIM's policy-expression compiler.
Likewise, a policy-fragment `PUT` can return `201 InProgress` before its
`Azure-AsyncOperation` later fails. Neither result proves that the production
include chain compiles. This applies equally to the model/priority policy
fragments and to the Speech Voice Live `onHandshake` operation policy
(`infra/policies/speech-voice-live.xml`); APIM's immutable `onHandshake` operation
does not support `validate-parameters`, so that policy relies on `choose` /
`return-response` / `set-query-parameter` instead, and still needs a real compile.

Before merging an APIM policy-fragment change, run the disposable full-chain
compiler harness against the target APIM:

```powershell
.\scripts\test-apim-policy-compiler.ps1 `
  -SubscriptionId <subscription-id> `
  -ResourceGroup <resource-group> `
  -ServiceName <apim-service-name>
```

The harness creates uniquely named temporary copies of every generated
model-policy fragment and a collision-free temporary API, waits for each
fragment's async compiler operation, and applies the generated API wrapper with
the complete fragment chain in production order. Its `finally` path deletes
only those exact temporary names and verifies that all are absent. It never
modifies a production API, policy, or fragment. A successful full-chain compile
plus cleanup verification is the decisive APIM service validation. Running this
harness — for either the model/priority fragments or the Speech Voice Live
policy — requires its own separate, explicit approval; it is never triggered
automatically because it creates temporary Azure resources.

#### Gateway canary and rollback

`ca-proxy` uses `activeRevisionsMode: Single`, so this template does not provide a
same-app weighted canary. Use a parallel azd environment/resource group as the
canary:

1. deploy the branch to the parallel environment with a separate proxy hostname;
2. verify `/startup`, `/liveness`, and `/readiness` on the proxy revision;
3. send one non-streaming and one streaming model request through the proxy URL;
4. run the authenticated app-path Voice Live canary through the API relay for each
   enabled provider/model, then perform a signed-in browser microphone retest, and
   confirm each one's upstream is the matching APIM WebSocket URL
   (`/openai/realtime` for `azure_openai`, `/speech/voice-live/realtime` for
   `speech_voice_live`), never the proxy;
5. verify APIM logs show the proxy subscription for HTTP/SSE, the
   `/openai/realtime` subscription only for Azure OpenAI Realtime, and — when
   Speech Voice Live is enabled — its own distinct subscription only for
   `/speech/voice-live/realtime`; and
6. move the production DNS/custom-domain binding only after those checks pass.

Use an operator-obtained Entra API token in an environment variable, never a CLI
argument. The canary sends no audio or real conversation:

```powershell
python scripts/voice-live-canary.py `
  --url wss://<api-host>/api/voice/live `
  --origin https://<web-origin> `
  --provider azure_openai `
  --model gpt-realtime `
  --region eastus2 `
  --token-env AI4IA_VOICE_CANARY_TOKEN
```

Repeat without `--region` for each enabled Speech managed model. Success requires
`session.created` then `session.updated`; there is no automatic provider fallback.
Correlate failures with `voice_live_completion`: provider, model/usage target,
outcome, bounded protocol error or close metadata, source event, and directional
frame counts/event types. Telemetry must not contain credentials, raw frames,
audio, transcripts, prompts/history, or tool arguments/results. Direct APIM bare
handshakes are infrastructure diagnostics only and are not proof of the
authenticated app path.

Stage this work: offline script/unit/catalog/docs checks; reviewed zero-delete
what-if and policy compile under separate approval; deployment; authenticated
app-path canary; then a manual signed-in microphone/selector/transcript retest.
This runbook does not claim that a live canary, compiler, what-if, deployment, or
manual retest has been run.

For rollback, redeploy the known-good commit. If only the proxy image regressed,
restore the previous Container App revision while preparing the source revert. If
the APIM policy or subscription topology changed, revert and run `azd provision`
as well as `azd deploy`; shifting Container App traffic alone does not roll back
APIM.

For an API-policy-only emergency rollback, apply
`infra/policies/simplel7proxy-rollback-policy.xml` through an explicitly
authorized operator change. Bicep never deploys this preserved live policy.
Rolling back Speech Voice Live specifically does not need a policy revert at all:
setting `speechVoiceLiveEnabled=false` (or dropping `speech_voice_live` from
`voiceProviderAllowlist`) is immediate and non-destructive. The flag prevents app
wiring and fresh conditional Speech APIM/RBAC creation, but ARM Incremental mode
does not remove a Speech API, operation policy, subscription, named values, or
deterministic Speech-specific Foundry User assignment created by an earlier
deployment. The retained API is still subscription-key protected and the running
API has no Speech key, so it is unreachable through the app, but the retained
objects remain dormant privilege and inventory. No automatic teardown occurs; see
[`feature-enablement.md`](./feature-enablement.md#speech-voice-live-second-voice-provider).

For a managed-model/selector regression, first narrow the Speech model allowlist
and default back to `gpt-realtime`, then restore the prior API/web Container App
revision. If necessary, narrow the provider allowlist to `azure_openai`. Do not
delete the shared APIM, AIServices account, role assignments, or other shared
resources as an incident rollback.

Full deactivation requires a separately approved targeted teardown. Refresh live
inventory, suspend or revoke `ai4ia-api-speech-voice-live` first, and then target
only the Speech API and operation policy, its two named values, and the
deterministic Speech-specific Foundry User role assignment. Review a targeted
what-if that contains no unplanned deletes and obtain explicit approval before
applying it. Never use complete deployment mode on the shared resource group or
shared APIM.

Generated model-policy fragments use content-addressed names. Incremental
deployment intentionally retains superseded generations for rollback. After a
new wrapper is stable, retain its active generation and one known-good rollback
generation; removing older unreferenced fragments is a separate destructive
operation that requires explicit approval.

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
   python scripts/gen-gateway-policy.py          # rewrites APIM deployment routing
   python scripts/gen-gateway-policy.py --check
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

Container Apps keeps prior revisions. The proxy is configured for single active
revision, so rollback means reactivating/redeploying the prior revision rather
than weighted traffic splitting.

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

### 7.3 Speech Voice Live fails startup or handshake

Symptom — the API refuses to start with an `AI4IA_SPEECH_VOICE_LIVE_*`/`AI4IA_VOICE_PROVIDER_ALLOWLIST`
error, or a Speech Voice Live connection gets a bounded handshake failure instead
of audio.

Cause and fix, by message:

| Error | Cause | Fix |
|---|---|---|
| `speech_voice_live requires AI4IA_SPEECH_VOICE_LIVE_ENABLED=true` | Provider listed in the allowlist without the feature flag | Set `AI4IA_SPEECH_VOICE_LIVE_ENABLED=true` or drop it from the allowlist |
| `Speech Voice Live requires AI4IA_REALTIME_ENABLED=true` | Speech enabled while the master Voice Live gate is off | Enable `AI4IA_REALTIME_ENABLED` first, or leave Speech off |
| `Speech Voice Live requires AI4IA_VOICE_PROVIDER_ALLOWLIST to include speech_voice_live` | Flag on but allowlist missing the provider | Add `speech_voice_live` to `AI4IA_VOICE_PROVIDER_ALLOWLIST` |
| `Speech Voice Live requires AI4IA_SPEECH_VOICE_LIVE_BASE_URL and AI4IA_SPEECH_VOICE_LIVE_GATEWAY_API_KEY` | One or both are empty | Confirm the gateway module outputs are wired through; do not hand-enter these |
| `Speech Voice Live requires an APIM-style HTTPS/WSS base URL at /speech/voice-live` | URL is not HTTPS/WSS, missing a host, wrong path, or points at `*.services.ai.azure.com` / `*.cognitiveservices.azure.com` directly | The URL must be the shared APIM gateway's `/speech/voice-live` path, never a Foundry/AIServices hostname |
| `Speech Voice Live requires a distinct gateway key; do not reuse ...` | The Speech key equals the Azure OpenAI realtime key or the model-gateway key | Regenerate/rotate so all three keys are distinct |
| Handshake fails with a bounded, provider-neutral error and no audio | The Speech Voice Live APIM API, its subscription, or the selected AIServices account's RBAC/audience is unhealthy | Check APIM diagnostics for the `speech-voice-live-realtime` API; confirm the managed identity still holds Cognitive Services User + Foundry User on the selected account and that the audience matches (see [`configuration-reference.md`](../configuration-reference.md#speech-voice-live-second-voice-provider)) |

Speech Voice Live never falls back to Azure OpenAI, the proxy, another host, or
Consumption APIM on failure — a failed Speech connection surfaces a bounded error
to the browser rather than silently degrading to a different provider or
deployment.

The safe protocol-error/close capture, correlation/outcome/frame-count completion
record, deterministic cleanup, retry messaging, and isolated inline selectors are
confirmed code-level diagnostics/UX fixes. They do **not** establish a root cause
for any Azure OpenAI Realtime failure. Until the authenticated canary and manual
signed-in microphone retest produce correlated evidence, an Azure OpenAI upstream
service or model cause remains unproven.

## APIM Basic v2 migration guardrail

The active model/realtime/MCP plane is the existing `apim-mcp-<workload>-<environmentName>` Basic v2 service (capacity 1). `apimcore.bicep` owns its identity and single diagnostic setting; `mcpgateway.bicep` preserves the MCP children and `gateway.bicep` adds model/realtime children through the shared contract, including the additive `speech_voice_live` WebSocket API (`/speech/voice-live/realtime`) and its own distinct subscription (`ai4ia-api-speech-voice-live`) alongside the existing `/openai/realtime` API and subscription. Reusing this paid service adds no roughly $150 APIM base cost, but MCP, HTTP/SSE, and both voice providers share its blast radius and resilience posture. The Consumption APIM and all children remain unchanged/inactive rollback with no active traffic — it never receives Speech Voice Live traffic either. MCP uses an MCP-only product/subscription, so its key cannot call model/realtime APIs; equally, neither voice provider's key can call the other's API, the model API, or the MCP plane. Configure APIs, policies, keys, and Foundry RBAC before caller revisions update. Review a zero-delete what-if; delete Consumption only in a separately approved post-stabilization change.

The superseded PR-only `apim-v2-*` design was never deployed. The corrected what-if
must contain no `apim-v2-*` creation. If resource inventory unexpectedly finds such
a service, stop and handle it through a separate explicitly approved cleanup rather
than folding a deletion into this migration.

### Current 403 inventory/what-if blocker (Speech Voice Live)

As of this writing, the available identity cannot read the target subscription or
resource group (`403 AuthorizationFailed` on a direct read of
`rg-ai4ia-slurmfactory`), so Speech Voice Live's account kind/endpoint/location,
APIM SKU/capacity/identity/APIs/subscriptions/backends, roles, provider
registration, policy support, health, and quotas/limits have **not** been
independently verified against live Azure state — the supplied production
inventory is expected context, not confirmed fact. `speechVoiceLiveEnabled`
therefore stays `false` in the checked-in parameters, and the following remain
required, separately authorized, and outstanding before any deployment can be
proposed:

1. An authorized identity regains subscription/resource-group read and
   `Microsoft.Resources/deployments/whatIf/action` access and re-verifies the
   selected `eastus2` AIServices account and shared APIM against live state.
2. The live APIM WebSocket policy compiler
   (`scripts/test-apim-policy-compiler.ps1`) validates the Speech Voice Live
   `onHandshake` operation, under its own separate approval, with verified
   temporary-resource cleanup.
3. A zero-delete production what-if is run and reviewed, containing no deletes,
   replacements, APIM SKU changes, or legacy Consumption mutations.

If subscription/resource-group read or `whatIf/action` remains `403`, if compiler
validation fails, or if what-if includes a delete/replace/APIM-SKU-change/legacy
Consumption mutation or unplanned resource/RBAC creation: **stop and do not
deploy.** Live APIM compiler validation, production deployment, and merging the
implementation to `main` are each separate, explicit approvals this repository's
documentation cannot substitute for.

### Post-stabilization Consumption cleanup

The two APIM services are a temporary migration overlap, not the steady-state design.
Delete the Consumption service only in a separate destructive change after:

1. HTTP and SSE smoke tests pass through SimpleL7Proxy -> shared Basic v2 APIM;
2. FastAPI -> shared Basic v2 APIM returns WebSocket 101 and completes a real voice turn
   for each enabled provider;
3. gateway logs confirm only `apim-mcp-*` receives active traffic for the agreed stabilization period;
4. operators confirm the Consumption HTTP rollback is no longer required; and
5. a deletion-specific what-if is reviewed and explicitly approved.

Realtime has no working Consumption rollback path for either voice provider.
During stabilization, HTTP/SSE rollback means restoring the previous proxy
revision/key that targets Consumption; do not delete or mutate the legacy APIs,
fragments, policies, subscriptions, or identity before the cleanup approval.
