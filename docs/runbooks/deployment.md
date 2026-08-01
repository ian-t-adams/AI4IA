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
   DNS provider, and verify against a **public** resolver — a corporate or ISP
   resolver can serve a stale answer for the whole TTL, and Azure reads public DNS.
2. Confirm the target Container Apps managed certificate names if adopting
   existing certs.
3. Set all four custom-domain repository variables before `azd provision`.
4. Treat missing variables as an outage risk, not a harmless omission.
5. **Binding a hostname for the first time?** It needs a one-time registration
   before Bicep can issue its certificate — see §3 step 3a. CI does this
   automatically; a local `azd provision` needs `az containerapp hostname add`
   run first, or the provision fails with `RequireCustomHostnameInEnvironment`.

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

### 2.7 Application Entra app registrations (required when `AI4IA_AUTH_PROVIDER=entra`)

The **deployment** identity in §2.1 only lets the pipeline provision Azure. Production
*user* sign-in is separate: it needs **two Microsoft Entra app registrations** that the
Bicep does **not** create (they are tenant objects, not subscription resources). The
`AI4IA_ENTRA_*` repo variables in §2.3 are pointers to these apps — set them to empty
values and they reference nothing, so every authenticated request returns `401`. Create
them once per tenant:

> **Automated:** `scripts/provision-entra-apps.ps1` does all of the below and prints the
> repo-variable values. Dry-run first, then apply:
> ```powershell
> ./scripts/provision-entra-apps.ps1 -WebRedirectUri https://<web-host>,http://localhost:3000
> ./scripts/provision-entra-apps.ps1 -WebRedirectUri https://<web-host> -AdminUpn you@tenant -Apply
> ```
> The manual steps below document exactly what it creates.

1. **API app registration** — the audience the API validates (`aud`, `iss`, `tid`;
   see `app/api/src/ai4ia_api/auth/entra.py`). Expose a delegated scope named
   `access_as_user` and set its access-token version to 2.

   ```powershell
   # API app: exposes api://<api-app-id>/access_as_user
   $api = az ad app create --display-name "AI4IA API (<env>)" `
     --sign-in-audience AzureADMyOrg | ConvertFrom-Json
   az ad app update --id $api.appId --identifier-uris "api://$($api.appId)"
   # Set requestedAccessTokenVersion=2 and add the access_as_user scope in the
   # portal (App registrations -> Expose an API -> Add a scope), or via Graph.
   ```

2. **Web SPA app registration** — the browser MSAL client. Add a **Single-page
   application** redirect URI equal to the web origin (the vanity host, e.g.
   `https://ai4ia.<domain>`, plus `http://localhost:3000` for local dev), and grant it
   delegated permission to the API app's `access_as_user` scope (grant admin consent).

   `az ad app create` has **no SPA redirect flag** (it only offers `--web-redirect-uris`
   and `--public-client-redirect-uris`); `spa.redirectUris` is settable only through
   Microsoft Graph, so create this one via `az rest`:

   ```powershell
   $body = @{
     displayName    = "AI4IA Web (<env>)"
     signInAudience = 'AzureADMyOrg'
     spa            = @{ redirectUris = @("https://ai4ia.<domain>", "http://localhost:3000") }
   } | ConvertTo-Json -Depth 5
   $body | Set-Content -Path ./web-app.json -Encoding utf8
   $web = az rest --method POST --url "https://graph.microsoft.com/v1.0/applications" `
     --headers "Content-Type=application/json" --body '@./web-app.json' | ConvertFrom-Json
   Remove-Item ./web-app.json
   ```

   > Granting **admin consent** needs Privileged Role Administrator, Cloud Application
   > Administrator, or Global Administrator. Subscription **Owner is not sufficient** — a
   > common surprise in MCAPS-managed tenants, where ARM ownership and directory roles are
   > separate planes. See "Is admin consent actually required?" below before chasing a role.

#### Is admin consent actually required? (usually not)

`provision-entra-apps.ps1` prints a verdict on this, but the reasoning matters because the
portal is genuinely ambiguous here — it shows a **Grant admin consent** control in two
different blades and hedges about the "Admin consent required" column.

Admin consent is **optional** when both of these hold:

1. **The scope is user-consentable.** `access_as_user` is created with scope type `User`,
   which is what makes API permissions show **Admin consent required: No**. (Type `Admin`
   would make it mandatory.) Check with:
   ```powershell
   az ad app show --id <api-app-id> --query "api.oauth2PermissionScopes[].{value:value,whoCanConsent:type}"
   ```
2. **The tenant lets users consent for themselves.** True when the default user role is
   assigned any `ManagePermissionGrantsForSelf.*` permission-grant policy:
   ```powershell
   az rest --method GET --url "https://graph.microsoft.com/v1.0/policies/authorizationPolicy" `
     --query "defaultUserRolePermissions.permissionGrantPoliciesAssigned"
   ```
   An empty result means user consent is switched off and admin consent becomes the **only**
   way anyone signs in.

When both hold, the entire cost of skipping admin consent is a one-time per-user prompt
("Access AI4IA as the signed-in user") at first sign-in. The web app requests only this one
scope — no Microsoft Graph permissions — so that prompt is a single line. Granting admin
consent later just removes the prompt; it is a UX improvement, not a prerequisite.

If you do need it and the **App registrations → API permissions** link is greyed out, that
is the portal telling you the signed-in account lacks the directory role. The
**Enterprise applications → Security → Permissions** blade renders the same action as an
enabled-looking blue button, but it calls the same API and fails the same way — it is not a
second, lower-privileged path. Two things that also do *not* help, despite looking relevant:

- **Expose an API / App roles** on the *web* app. The web app is a client; it exposes
  nothing. `access_as_user` lives on the **API** app and is already exposed. AI4IA gates
  admins with the `AI4IA_ADMIN_SUBJECTS` oid allowlist, not Entra app roles.
- **Roles and administrators** on the app registration. That delegates *administration of
  that app object*, and assigning a directory role there itself requires Privileged Role
  Administrator or Global Administrator — so it cannot bootstrap you out of the gap.

> **Who may sign in** is a separate control from consent, and is easy to conflate. A new
> service principal has `appRoleAssignmentRequired: false`, so *every* user in the tenant
> can sign in once consent exists. To restrict it, set Enterprise applications → the app →
> Properties → **Assignment required = Yes**, then assign users/groups. The app's **owner**
> can change this with no directory role:
> ```powershell
> az ad sp update --id <web-app-id> --set appRoleAssignmentRequired=true
> ```

> The **"Azure AD Graph / ADAL are deprecated"** banner on app registrations is generic and
> does not apply here: the web app uses MSAL (`@azure/msal-browser`, `@azure/msal-react`),
> and `provision-entra-apps.ps1` talks to Microsoft Graph (`graph.microsoft.com/v1.0`), not
> the retired Azure AD Graph (`graph.windows.net`).

Then map them to the repo variables (§2.3):

| Repo variable | Value |
|---|---|
| `AI4IA_ENTRA_TENANT_ID` | the tenant GUID (also `AZURE_TENANT_ID`) |
| `AI4IA_ENTRA_AUDIENCE` | the **API** app id GUID (the code accepts `api://<guid>` too) |
| `AI4IA_ENTRA_API_SCOPE` | `api://<api-app-id>/access_as_user` |
| `AI4IA_ENTRA_WEB_CLIENT_ID` | the **web SPA** app id GUID |
| `AI4IA_ADMIN_SUBJECTS` | comma-separated `oid` of the admin user(s); gates `/api/admin/*` |

Until all four `AI4IA_ENTRA_*` variables are set to real registrations, keep
`AI4IA_AUTH_PROVIDER` unset/`dev` (the demo flow). With `AI4IA_AUTH_PROVIDER=entra` the
API fails closed on a missing audience/tenant, and the web app falls open to the dev
flow if any `ENTRA_*` value is missing (see `app/web/.env.example`).

## 3. Moving to a new subscription or tenant (1:1 standup)

The stack is data-driven, so standing it up in a **new subscription/tenant** is a small set of
config edits plus the normal deploy — no code changes. What varies per environment is centralized:

| What | Where | Notes |
|---|---|---|
| Environment name | `AZURE_ENV_NAME` repo/azd var | Feeds `environmentName`; names the RG (`rg-ai4ia-<env>`), Foundry accounts/projects (`mf-aiforia-<env>-<region>-<suffix>`), Container Apps, etc. Globally-unique names additionally carry `uniqueString(subscription().id, environmentName)` — see [naming](../naming-and-tagging.md) and §7.6. |
| Subscription / tenant / region | `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`, `AZURE_LOCATION` repo vars | See §2.3. |
| CI/CD deployment identity | `AZURE_CLIENT_ID` repo var | **Not portable.** The federated credential is a tenant object; an identity from the old tenant cannot authenticate to the new one, so `azure/login` fails before any Bicep runs. Recreate per §2.1–2.2 (managed identity + `repo:<owner>/<repo>:environment:production` subject + `Contributor` and `Role Based Access Control Administrator`). |
| Application Entra app registrations | `AI4IA_ENTRA_AUDIENCE`, `AI4IA_ENTRA_API_SCOPE`, `AI4IA_ENTRA_WEB_CLIENT_ID`, `AI4IA_ENTRA_TENANT_ID` | **Not portable** — directory objects, not subscription resources, so nothing in Bicep creates them. Recreate with `scripts/provision-entra-apps.ps1` (§2.7). Carrying the old app IDs over leaves `AI4IA_AUTH_PROVIDER=entra` pointing at audiences that do not exist in the new tenant: the stack provisions green and every authenticated request then returns `401`. |
| Admin subjects | `AI4IA_ADMIN_SUBJECTS` repo var | **Not portable, and fails silently.** An `oid` identifies a *user object in one directory*; the same human signing into a new tenant gets a **different** `oid`. A carried-over value is simply an id that matches nobody — no error anywhere, the operator just quietly stops being an admin (losing `/api/admin/*` **and** the P0 gateway priority band). Re-read it in the new tenant with `az ad signed-in-user show --query id -o tsv`. |
| Model deployment-name token | `infra/models.json` → `naming.subscriptionToken` | Stamped into every model deployment name (`{model}-<token>-<region>-<sku>`). Read by bicep **and** the runtime catalog. |
| Foundry account/project token | `infra/models.json` → `naming.foundryToken` | Names `mf-<token>-<env>-<region>` and the toolbox project endpoint. |
| Postgres region (temporary) | `AI4IA_POSTGRES_LOCATION` | Required only while the legacy migration source/document-index fallback remains (see §7.1). |
| API Center region | `AI4IA_API_CENTER_LOCATION` | Only if `enablePrivateToolCatalog=true`; not available in every region (see §7.2 / the API Center note). |
| Custom domains | `AI4IA_*_CUSTOM_DOMAIN` / `*_MANAGED_CERT_NAME` | Leave **all four empty for the first provision in a new tenant** — see step 3a below, this is ordering-sensitive and a wrong order fails the deploy. Leave empty permanently for a vanilla hostname; see §2.5. The values in §2.5's table are **this deployment's**, not portable — a new tenant has its own hostnames and certs. |
| APIM publisher mailbox | `AI4IA_APIM_PUBLISHER_EMAIL` | Must be an operator-owned address in the new tenant. It receives APIM service notices, including **managed-certificate expiry** for the custom domains bound in step 3a. `validate-feature-prereqs.py` warns while it is still the `@example.com` placeholder. |
| Owner / cost-center tags | `AI4IA_OWNER`, `AI4IA_COST_CENTER` | Accountability tags stamped on every resource; see [`naming-and-tagging.md`](../naming-and-tagging.md). |

> **Setting a repo variable is only half the wiring.** `infra/main.parameters.json` reads
> every knob as `${VAR=default}`, and azd substitutes **only variables that are actually
> present in the environment** — it does not read GitHub repo variables. Any parameter that
> `deploy.yml` does not export in its `env:` block therefore deploys its placeholder
> default no matter what the repo variable says, with no warning. This was live: the first
> standup provisioned APIM with `ai4ia@example.com` and tagged every resource
> `owner=ai4ia-operator` while both repo variables were set correctly.
> `test_every_azd_parameter_token_is_reachable_from_ci` now fails CI if a parameter token
> is added without a matching export.

> **Exporting a variable that has no repo variable is safe — do not add `|| 'default'`
> fallbacks to "protect" against it.** GitHub Actions expands `${{ vars.MISSING }}` to an
> *empty string* rather than omitting the variable, so it is reasonable to worry that
> exporting an unset knob overwrites a non-empty parameter default with `''`. It does not:
> azd's `${VAR=default}` resolves an empty value to the **default**, i.e. it behaves like
> POSIX `${VAR:-default}`, not `${VAR=default}`. Verified live against
> `main`'s own workflow — with `AI4IA_POSTGRES_LOCATION`, `AI4IA_SEARCH_LOCATION`, and
> `AI4IA_API_CENTER_LOCATION` all *unset* as repo variables, the resulting subscription
> deployment resolved them to `centralus`, `eastus`, and `eastus` respectively:
>
> ```powershell
> az deployment sub show -n <name> --query properties.parameters -o json
> ```
>
> This matters most for `AI4IA_POSTGRES_LOCATION`, because `main.bicep` derives
> `postgresEnabled = !empty(postgresLocation)` — had empty won, exporting the unset
> variable would have **disabled and torn down the Postgres flexible server**. The
> `|| 'literal'` fallbacks that some entries do carry (`AI4IA_PROXY_WORKERS`,
> `AI4IA_MEMORY_STORE`) exist to pin a value that differs from the parameter default, not
> to guard against empty. Re-confirm with the query above rather than re-deriving this
> from first principles.

Deliberately **not** in that list, because they need no per-tenant edit:

- **Voice Live Origin allowlist.** Bicep derives it from the web app this deployment
  actually creates (Container Apps default FQDN + `webCustomDomain` when bound), so it
  is correct in a new tenant with no configuration. `AI4IA_REALTIME_ALLOWED_ORIGINS`
  only *adds* origins. (This used to be a hardcoded hostname in
  `infra/main.parameters.json` — a stale value still satisfied the API's non-empty
  allowlist startup check, so the stack came up green and then rejected every browser.)
- **Built-in Azure role IDs** in `infra/modules/*.bicep` are the same GUIDs in every
  tenant.
- **Operator scripts.** None carry a default subscription, resource group, or purge
  filter. `status-snapshot.ps1` resolves its target from the selected azd environment
  (falling back to the current `az` context); `inventory.ps1`, `teardown.ps1`,
  `purge-soft-deleted.ps1`, and `seed-models.ps1` require the target explicitly.

Procedure:

0. **Preflight the target subscription.** Both checks below are read-only and take
   under a minute. They exist because the failures they catch surface *late* —
   `azd provision` creates the resource group, Foundry accounts, gateway, and data
   tier first, so a missing provider or an unavailable model kills the run after
   the slow, expensive part already succeeded, leaving a half-built stack.

   ```powershell
   az login
   az account set --subscription <new-subscription-id>

   python scripts/check-resource-providers.py     # add --register to fix
   python scripts/check-model-availability.py
   ```

   `check-resource-providers.py` derives the required namespaces from
   `infra/**/*.bicep`, so it cannot drift when a module adds a resource type. An
   untouched subscription typically has **most of them unregistered** — a fresh
   one measured 18 of them missing. `--register` requests them all and waits for
   `Registered`, which is asynchronous and can take several minutes.

   `check-model-availability.py` compares `infra/models.json` against what the
   subscription is actually entitled to deploy, per region, on **two** axes that
   fail independently:

   * **Availability** — is the model offered here? Limited-access models need an
     approved request and partner models need the Marketplace offer enabled.
     Version mismatches are reported as warnings, not errors, because Azure
     commonly rolls a retired pinned version forward.
   * **Quota** — is there capacity left? A brand-new subscription is offered
     nearly everything but ships small default quotas, so availability passes and
     the deployment still dies on `InsufficientQuota`. Requested capacity is
     summed per model+SKU, because quota is per subscription+region+model+SKU and
     several deployments draw down one shared counter.

   Quota counters are not named after the models they meter — they carry a
   publisher prefix the catalog never mentions (`OpenAI.` vs `AIServices.`) and
   respell the model (`model-router` → `ModelRouter`, `o3-deep-research` →
   `o3-DeepResearch`, `Cohere-rerank-v4.0-pro` → `Cohere-Rerank-V4-Pro`). The
   script reconciles these; a counter it still cannot match is a **warning**, not
   an error, because the absence is ambiguous — verify by hand with
   `az cognitiveservices usage list -l <region>`. Use `--skip-quota` to check
   availability alone.

   If a model is genuinely unavailable, either request access or drop its
   deployment from `infra/models.json` and re-run `python scripts/gen-model-catalog.py`
   (plus the generators in step 2). If it is merely out of quota, request an
   increase or lower that deployment's `capacity`.

1. Set the repo variables for the new subscription/tenant/env (§2.3), plus
   `AI4IA_POSTGRES_LOCATION` while PostgreSQL is retained and (if used)
   `AI4IA_API_CENTER_LOCATION` to regions valid there.
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
3a. **Custom domains: bind them *after* the first provision, not during it.**

   Set `AI4IA_WEB_CUSTOM_DOMAIN`, `AI4IA_PROXY_CUSTOM_DOMAIN`, and both
   `*_MANAGED_CERT_NAME` variables to **empty** before step 3, then re-provision
   once DNS has moved. This is not a preference — the first provision *fails*
   otherwise, and it fails late.

   Why: when `webCustomDomain`/`proxyCustomDomain` is non-empty, `web.bicep` and
   `gateway.bicep` create a `managedCertificates` resource with
   `domainControlValidation: 'CNAME'`. Azure only issues that certificate after it
   can verify the hostname resolves to **this** environment — via the `CNAME` to
   the app's `*.azurecontainerapps.io` FQDN, or the `asuid.<host>` `TXT` record
   holding this app's `customDomainVerificationId`. During a migration both still
   point at the *old* tenant's app, and the new app does not exist yet to point
   them at. So issuance fails, the ARM resource fails, and the whole `azd up`
   fails — after the Foundry accounts, gateway, and data tier are already built.

   Order that works:

   1. Provision with all four domain variables empty. Everything comes up on the
      default `*.azurecontainerapps.io` hostnames and is fully usable.
   2. Read the new coordinates:

      ```powershell
      az containerapp show -g rg-ai4ia-<env> -n ca-web-<env> `
        --query "{fqdn:properties.configuration.ingress.fqdn, verificationId:properties.customDomainVerificationId}"
      ```

   3. **Cutover DNS** at your provider: point `CNAME <host>` at the new `fqdn`
      and set `TXT asuid.<host>` to the new `verificationId`. Repeat for the proxy
      hostname. Allow the old TTL to expire before continuing — validation reads
      public DNS, not your zone file.
   4. Set `AI4IA_WEB_CUSTOM_DOMAIN` / `AI4IA_PROXY_CUSTOM_DOMAIN` and re-provision.
      Leave `*_MANAGED_CERT_NAME` empty: with no pinned name Bicep derives a stable
      one (`mc-<host-with-dashes>`), which is what you want in a tenant that has no
      pre-existing cert to adopt. Pinning a name copied from the old tenant does
      not adopt anything — that cert lives in the old tenant's managed environment
      — it just names the new one confusingly.

      **The very first bind of a hostname takes two phases, and Bicep can only do
      the second.** Container Apps refuses to create a managed certificate whose
      subject is not *already* a custom hostname somewhere in the environment:

      ```text
      RequireCustomHostnameInEnvironment: Creating managed certificate requires
      hostname '<host>' added as a custom hostname to a container app or route
      in environment '<env>'
      ```

      `web.bicep` / `gateway.bicep` declare the certificate and the ingress binding
      together, and ARM creates the certificate first (the app depends on
      `webCert.id`). So the hostname that would satisfy the check is introduced by
      the resource that is blocked *on* the check. No ordering inside Bicep can
      break that cycle — the registration has to happen out of band, once.

      The GitHub Actions deploy does this for you: the **Preflight custom-domain
      bindings** step runs `az containerapp hostname add` for any hostname not yet
      registered, before `azd provision`. Just set the variables and re-run.

      Provisioning locally, do phase one by hand first:

      ```powershell
      az containerapp hostname add -g rg-ai4ia-<env> -n ca-web-<env> `
        --hostname ai4ia.example.com
      az containerapp hostname add -g rg-ai4ia-<env> -n ca-proxy-<env> `
        --hostname genaiproxy.example.com
      azd provision
      ```

      `hostname add` registers the hostname with `bindingType: Disabled` and no
      certificate, which is exactly the precondition the certificate needs. It is
      idempotent, and it is also where Azure validates domain control — so a DNS
      mistake surfaces in seconds here instead of ~20 minutes into a provision.
      `azd provision` then issues the certificate and flips the binding to
      `SniEnabled`.
   5. Optional: once issued, record the actual names back into
      `AI4IA_*_MANAGED_CERT_NAME` so later deploys are explicit rather than derived.

   Because the binding lives in Bicep rather than being added imperatively, once
   step 4 succeeds it is durable — subsequent deploys re-assert it instead of
   wiping it (§2.5). Phase one is a one-time bootstrap per hostname, not a
   recurring step: afterwards the hostname is already registered and the preflight
   is a no-op.
4. **Foundry toolbox (data-plane, if used):** the toolbox is not created by `azd up`. After the
   deploy, run `python scripts/provision-foundry-toolbox.py --create` against the new project (the
   `infra/mcp-servers.json` entry is already portable — its APIM upstream URL is computed by bicep
   from the new project endpoint). See [`../foundry-toolbox.md`](../foundry-toolbox.md).
5. **Break-glass ops scripts** (`scripts/inventory.ps1`, `teardown.ps1`,
   `purge-soft-deleted.ps1`, `seed-models.ps1`) take their target explicitly — they have
   no default subscription, resource group, or purge filter, so they cannot silently act
   on the environment you moved away from. `purge-soft-deleted.ps1` reads
   **subscription-wide** soft-delete lists, so its mandatory `-NameFilter` is what keeps
   a purge scoped to this stack.
6. **Regenerate the published status/inventory data** (only if you publish the portal):
   `./scripts/status-snapshot.ps1` resolves the subscription, resource group, and probe
   URLs from the selected azd environment, so run `azd env select <new-env>` first. The
   checked-in `site/data/*.js` snapshots still describe the *previous* environment until
   you do.

Clean-room notes for a brand-new subscription/tenant:

- **Entra app registrations** (§2.7) are per-tenant and not created by `azd`. Create the API + web
  SPA apps and set the four `AI4IA_ENTRA_*` variables before enabling `AI4IA_AUTH_PROVIDER=entra`,
  or the app returns `401`.
- **Resource provider registration** — an untouched subscription has most of the required
  providers unregistered, and provisioning fails partway through when it hits the first
  one. Run `python scripts/check-resource-providers.py --register` (step 0 above) rather
  than registering by hand: the script derives the full set from `infra/**/*.bicep`, so it
  stays correct as modules change, and it waits for registration to actually complete.
- **Activate memory** — `AI4IA_MEMORY_STORE` defaults to `disabled` in `deploy.yml` (fail-closed).
  A greenfield stand-up has no legacy `mem0` data to migrate, so set the `AI4IA_MEMORY_STORE` repo
  variable to `cosmos` and deploy — no migration runbook needed. (The [memory-migration
  runbook](./memory-migration.md) applies only when moving *existing* `mem0`/PostgreSQL memories.)
- The first provision can hit the Cosmos vector-capability race in §7.4; re-running `azd provision`
  resolves it.

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

Cause — the temporarily retained Postgres Flexible Server (legacy memory migration
source and optional document-index fallback) is being provisioned in a region
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
| `Speech Voice Live requires a distinct gateway key; do not reuse ...` | The Speech key equals the Azure OpenAI realtime key or FastAPI's proxy-ingress (`AI4IA_MODEL_GATEWAY_API_KEY`) key | Regenerate/rotate the FastAPI-held keys; the separate proxy-to-model-APIM key remains held only by SimpleL7Proxy |
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

### 7.4 `Provision infrastructure` fails: Cosmos "Container Vector Policy ... capability has not been enabled"

Symptom — `azd provision` fails creating the Cosmos `memories` container with
`BadRequest ... "A Container Vector Policy has been provided, but the capability has not
been enabled on your account"` (request URI `/dbs/ai4ia/colls`).

Cause — `infra/modules/data.bicep` declares the account capability
`EnableNoSQLVectorSearch` **and** the vector `memories` container in the same
deployment. `EnableNoSQLVectorSearch` must propagate to the Cosmos **data plane** before
a vector container can be created; when the capability is newly added to an existing
account (the upgrade path) the container create can race ahead of propagation and fail.
A brand-new (greenfield) account usually succeeds on the first pass because account
creation itself gives the capability time to activate.

Fix — enable the capability out-of-band, then **re-run the deploy** (`azd provision` is
idempotent, exactly like §7.1/§7.2):

```powershell
# Preserve EnableServerless; add the vector capability. Takes ~1-2 min to propagate.
az cosmosdb update -g "rg-ai4ia-<env>" -n "<cosmos-account>" `
  --capabilities EnableServerless EnableNoSQLVectorSearch

# Then re-run the deploy workflow (or `azd provision` locally). The memories container
# is created once the capability is active; the second run is otherwise a no-op.
gh workflow run deploy.yml -f provision=true --ref main
```

If the account already has the capability but the container still doesn't exist, create
it directly with the exact policy from `data.bicep` (partition key `/userId`, vector
`/embedding` float32/cosine/3072 `quantizedFlat`, `/embedding/*` excluded, TTL `-1`) via
an ARM `PUT` to `.../sqlDatabases/ai4ia/containers/memories?api-version=2024-11-15`; a
subsequent `azd provision` reconciles it idempotently.

### 7.5 Every deploy fails validation with `DeploymentActive` (cancelled/timed-out provision)

Symptom — `azd provision` fails within ~30s of "Validating deployment", and **every**
subsequent run fails the same way:

```text
DeploymentActive: The deployment with resource id '.../deployments/<name>' cannot be saved,
because this would overwrite an existing deployment which is still active. ...
The previous deployment was started at '<t0>' ... and will expire at '<t0 + 7 days>'.
```

Cause — a previous run was **cancelled** rather than failed, most often by the job's
`timeout-minutes` (GitHub reports a timed-out job as `cancelled`). Killing the runner does
not stop ARM: the in-flight deployment stays **active server-side for up to 7 days**, and
ARM refuses to start another deployment with the same name. So one cancelled provision
wedges the subscription for a week — this is not a transient error that clears on retry,
and it is why `deploy.yml` sets a deliberately generous `timeout-minutes` (see the comment
on that setting).

Fix — cancel the orphaned deployment, then re-run. Cancelling is safe: the run that owned
it is already gone, and `azd provision` is idempotent.

```powershell
$rg = "rg-ai4ia-<env>"

# 1. Find what is still running (this is the deployment named in the error).
az deployment group list -g $rg `
  --query "[?properties.provisioningState=='Running'].{name:name, started:properties.timestamp}" -o table

# 2. Cancel each one. Repeat for nested deployments if more than one is Running.
az deployment group cancel -g $rg -n "<name-from-step-1>"

# 3. Confirm nothing is Running any more, then re-deploy.
az deployment group list -g $rg --query "[?properties.provisioningState=='Running'].name" -o tsv
gh workflow run deploy.yml -f provision=true --ref main
```

If the deployment is at **subscription** scope rather than inside the resource group, use
`az deployment sub list` / `az deployment sub cancel` instead. If a resource is genuinely
stuck mid-create (rather than the deployment record merely being orphaned), cancel still
succeeds but the resource may need deleting before the next provision can recreate it.

### 7.6 `ServiceAlreadyExists` / "The name … is already taken" (APIM, API Center, Foundry)

Symptom — the first provision into a **new** subscription fails on names that have always
worked, while the resource group is demonstrably empty:

```text
apimcore   ServiceAlreadyExists: Api service already exists: apim-mcp-ai4ia-<env>
apicenter  ValidationError: The name "apic-ai4ia-<env>" is already taken. Please choose another one.
```

Cause — these names are unique across **all of Azure**, not just the subscription, because
they back public DNS (`<name>.azure-api.net`, `<account>.services.ai.azure.com`). The name
is still held by the *previous* tenant's stack, which is not visible from the new
subscription at all. A template that omits a per-subscription suffix therefore deploys
forever in the subscription that first claimed the name and can never be stood up beside it.

Fix — already fixed in-template: `apim-mcp-*`, `apim-*`, `apic-*`, and the `mf-*` Foundry
accounts all interpolate `uniqueSuffix` (`uniqueString(subscription().id, environmentName)`).
`scripts/tests/test_bicep_naming.py` fails CI if any globally unique name loses it. If you
hit this on a resource type not yet covered, add it to `GLOBALLY_UNIQUE` in that test rather
than renaming the environment.

Note this changes resource names, so the old and new environments are independent — the new
subscription gets its own APIM/Foundry endpoints. Nothing in the generated runtime catalog
or gateway policy hardcodes these names; they flow from Bicep outputs.

### 7.7 `ServerIsBusy` on a PostgreSQL child resource

Symptom — the `data` deployment fails on a firewall rule, database, or configuration while
the server itself reports `Ready`:

```text
ServerIsBusy: Cannot complete operation while server 'psql-…' is busy processing
another operation. Try again later.
```

Cause — PostgreSQL flexible server serialises control-plane operations, and a server
configuration change (the `azure.extensions` update) leaves it internally busy after ARM has
already reported that child resource as succeeded. The template already chains its children
(`postgres → administrators → configurations → database → firewallRule`) so they never run
concurrently; this is the server settling *after* the chain's own dependency was satisfied,
which Bicep cannot express a wait for.

Fix — **re-run the provision.** This is genuinely transient and idempotent: the server is
`Ready` by then and the earlier steps are no-ops. It is not a wedged deployment, so §7.5
does not apply, but check that nothing is left `Running` before retrying.

```powershell
az postgres flexible-server show -g rg-ai4ia-<env> --name psql-… --query state -o tsv   # expect: Ready
gh workflow run deploy.yml -f provision=true --ref main
```

### 7.8 `ServiceModelDeprecating` on a model deployment

```text
models-eastus2  DeploymentFailed
  ServiceModelDeprecating: The model 'Format:OpenAI,Name:gpt-4.1-mini,Version:2025-04-14'
  is in deprecating state and cannot be used for new deployments.
```

Cause — Azure moved the model to the `Deprecating` lifecycle state. This blocks **new**
deployments while **existing** ones keep serving until the retirement date, which is what
makes it so easy to miss: the model is still listed by `az cognitiveservices model list`,
still has quota, and still works in the environment you already have. An incremental
deploy into an established environment never re-creates the deployment, so it never
fails. Only a **clean provision** — i.e. exactly a tenant/subscription migration — hits it.

Note the blast radius: ARM aborts the whole `models-<region>` nested deployment on the
first such model, so the error names one model even when several are affected. Check them
all in one pass rather than fixing one at a time.

Prevention — `scripts/check-model-availability.py` now checks lifecycle as a third axis
alongside availability and quota, and blocks on `Deprecating`/`Deprecated`. Run it before
any cold provision (step 0). It reports whether a deployable version exists to repin to,
or that the model must be removed.

Fix — if another version of the same model is `GenerallyAvailable`/`Preview`, repin
`version` in `infra/models.json`. If not (the whole family may go at once, as GPT-4.1 did),
remove the model and **migrate anything that referenced it by name**. That last step is the
dangerous one: `config.py memory_extraction_model` and `main.bicep
effectiveCodeInterpreterModel` both name a model directly, and an unresolvable
memory-extraction model degrades to `NoopMemoryService` with only a log line — memory
silently stops working. `docs/region-capability-matrix.md` tracks which models are
load-bearing for exactly this reason.

```powershell
python scripts/check-model-availability.py        # find every affected model at once
# edit infra/models.json, then:
python scripts/gen-model-catalog.py
python scripts/gen-gateway-policy.py
python scripts/validate-catalog.py
```

### 7.9 `InsufficientQuota` on a model deployment

```text
models-swedencentral  DeploymentFailed
  InsufficientQuota: This operation require 2 new capacity in quota
  "Microsoft.MAIImage.GlobalStandard", which is bigger than the current available
  capacity 0. The current quota usage is 2 and the quota limit is 2.
```

**Model quota is subscription-wide, not per-region — the usage API just reports it per
region.** This is the single most misleading thing about diagnosing it. `az cognitiveservices
usage list -l <region>` replicates one subscription-wide aggregate into *every* region's
response, so the same counter reads identically everywhere:

```powershell
foreach ($r in 'eastus2','swedencentral','westus') {
  az cognitiveservices usage list -l $r -o json |
    ConvertFrom-Json | Where-Object { $_.name.value -eq 'AIServices.GlobalStandard.MAI-Image-2.5' } |
    ForEach-Object { "{0,-14} used={1} limit={2}" -f $r, $_.currentValue, $_.limit }
}
```

That prints `used=2 limit=2` for all three — including **eastus2, which does not offer
MAI-Image at all**. The subscription's only deployment is in westus.

So a catalog can pass every per-region check and still fail: asking for capacity 2 in each
of two regions is fine region-by-region (2 ≤ 2 twice) but it is 4 against a shared limit of
2. Whichever region ARM reaches first wins and the other dies. **This is deterministic —
re-running only changes which region loses.**

Enforcement is not uniform, and the difference matters:

| Publisher | Enforced | Evidence |
| --- | --- | --- |
| `AIServices.*` (Microsoft) | subscription-wide | MAI-Image-2.5/-Flash/-Pro deployed in westus, then failed in swedencentral |
| `OpenAI.*` | per region | `gpt-image-1.5` holds a full 9-capacity deployment in eastus2 **and** swedencentral — 18 against a limit of 9, both succeeded |

`check-model-availability.py` encodes exactly that: a multi-region overcommit is an **error**
for non-OpenAI models and a **warning** for OpenAI ones.

**Triage.** Work out which of three cases you are in before changing anything:

1. **Subscription-wide overcommit** (the case above). Run the preflight — it reports it under
   `Subscription-wide quota (shared across regions)`. Fix by dropping a region from that model
   in `infra/models.json`, or request a quota increase. Never "just re-run".
2. **Capacity genuinely exceeds the limit** in a single region. Lower `capacity` or request an
   increase. Also blocking in the preflight.
3. **Transient.** `currentValue` includes in-flight reservations, so a rolled-back attempt
   keeps its capacity reserved briefly. Only plausible when the *total* fits — otherwise you
   are in case 1 and re-running will not help. Models whose `capacity` equals their `limit`
   have zero headroom and are the ones exposed to this; the preflight warns about each.

Do **not** treat a saturated `currentValue` in one region as proof of anything by itself. It
is a subscription-wide aggregate, it is clamped to the limit (`gpt-image-1.5` shows `9/9`
while 18 units are deployed), and it moves during a provision.

Remember ARM aborts the whole `models-<region>` nested deployment at the first failure, so
one error hides the rest. Re-running the preflight after a failure is the cheapest way to see
the full picture:

```powershell
az account set --subscription <the-right-one>   # the check follows your az context
python scripts/check-model-availability.py
```

### 7.10 Gateway returns `429 No Backends Available` for every request

The body is exactly:

```json
{"statusCode":429,"statusReason":"No Backends Available"}
```

**Do not start by looking at capacity.** This status is emitted by the SimpleL7Proxy
`on-error` fragment, and until the fix described below it was also emitted for failures
that never reached backend selection at all. The classic case is an APIM
**subscription-key rejection**: the key is missing, wrong, or sent under the wrong header
name, APIM aborts in its `authorization` stage, `on-error` runs before any inbound
fragment has built `listBackends`, and the empty list counts as "zero un-throttled
backends". A 401 is then reported as a retryable 429 that blames the model backends.

Tell the two apart by whether *any* request works, including the un-routed health path:

```powershell
# Correct header name. APIM uses Ocp-Apim-Subscription-Key -- api-key is Azure OpenAI's
# header and APIM ignores it, which reproduces this exact 429.
curl -s -D- -o- https://<apim>.azure-api.net/openai/status -H "Ocp-Apim-Subscription-Key: <key>"
```

`/openai/status` returns `{"status":"ok"}` from a `return-response` at the top of the first
inbound fragment. It touches no model backend, so:

| `/openai/status` | Meaning |
| --- | --- |
| `200 {"status":"ok"}` | Inbound policy is healthy. A 429 on a *model* call is a genuine capacity/throttle result. |
| `429 No Backends Available` | The request is failing **before** routing. Check the subscription key and header name first. |

Read `X-Policy-LastError` on the response — it now carries the real
`context.LastError` reason/message (for example `SubscriptionKeyNotFound`) even when the
inbound fragments never ran. A pre-routing failure also no longer sets `S7PREQUEUE` or
`retry-after-ms`, because retrying cannot fix a rejected credential.

If you need the underlying APIM error directly, take a gateway trace:

```powershell
# apiId must be a bare ARM resource id, not a management.azure.com URL.
az rest --method post --url "<apim-arm-id>/gateways/managed/listDebugCredentials?api-version=2023-05-01-preview" `
  --body '{\"credentialsExpireAfter\":\"PT1H\",\"apiId\":\"<apim-arm-id>/apis/openai\",\"purposes\":[\"tracing\"]}'
# send Apim-Debug-Authorization: <token>, read Apim-Trace-Id from the response, then:
az rest --method post --url "<apim-arm-id>/gateways/managed/listTrace?api-version=2023-05-01-preview" `
  --body '{\"traceId\":\"<id>\"}'
```

In the trace, inbound jumping straight to `on-error` with **no `include-fragment` entries**
is the signature of a pre-routing failure.

The proxy's own poller is a good second opinion — it calls `/openai/status` continuously
with the correct header:

```powershell
az containerapp logs show -g <rg> -n ca-proxy-<env> --tail 40 --format text
# [Poller]: ... Path: /openai/status Success: True ... Code: OK   <- proxy -> APIM is healthy
```

### 7.11 Model call returns `400 model_path_mismatch`

```json
{"error":{"code":"model_path_mismatch","message":"The deployment path must match the detected catalog model."}}
```

This is a **client-contract error, not a deploy failure**, and it is the single most
common way a hand-rolled verification call fails against a healthy gateway. A model call
through APIM must satisfy three things at once, and only the first is intuitive:

| # | Requirement | Wrong value produces |
| --- | --- | --- |
| 1 | `Ocp-Apim-Subscription-Key` header | `401 SubscriptionKeyInvalid` (see §7.10) |
| 2 | Path deployment is the **full** name — `gpt-5.6-luna-slurmfactory-eastus2-glbl`, not the catalog id `gpt-5.6-luna` | `400 model_path_mismatch` |
| 3 | `x-LLMModel` header, **equal to the path deployment** | `400 model_path_mismatch` |

Requirement 3 is the one that surprises people: the gateway resolves the backend from the
`x-LLMModel` **request header** (`modelHeaderName` in
`infra/policies/simplel7proxy-endpoints.xml`), *not* from the URL path and *not* from the
request body's `model` field. With no header the detected model is empty, it cannot equal
the path, and the guard rejects the call — so adding `"model": "..."` to the JSON body
looks like the obvious fix and changes nothing.

The guard itself is deliberate: it stops a caller from being billed against one deployment
while the body names another. Canonical call:

```powershell
$dep = "gpt-5.6-luna-slurmfactory-eastus2-glbl"   # az cognitiveservices account deployment list
$h = @{
  'Ocp-Apim-Subscription-Key' = $key
  'Content-Type'              = 'application/json'
  'x-LLMModel'                = $dep              # MUST match $dep in the path
}
$body = '{"messages":[{"role":"user","content":"Reply with exactly: pong"}],"max_completion_tokens":16}'
Invoke-WebRequest -SkipHttpErrorCheck -Method POST -Headers $h -Body $body `
  -Uri "https://<apim>.azure-api.net/openai/deployments/$dep/chat/completions?api-version=2025-04-01-preview"
```

A healthy response is `200` with `choices[0].message.content` = `pong`, `model` =
`gpt-5.6-luna-2026-07-09`, and `content_filter_results` **present with
`"filtered": false`** — that last part is the live proof that the annotate-only RAI policy
is attached (filtering disabled, annotation still returned). Missing
`content_filter_results` entirely means the policy is not applied; see
`scripts/tests/test_rai_policy.py`.

Deployment names are catalog-derived (`<model>-<subscriptionToken>-<region>-<sku>`), so
list them rather than reconstructing them by hand:

```powershell
az cognitiveservices account deployment list -g <rg> -n <foundry-account> --query "[].name" -o tsv
```

### 7.12 `RequireCustomHostnameInEnvironment` on a managed certificate

```text
RequireCustomHostnameInEnvironment: Creating managed certificate requires hostname
'<host>' added as a custom hostname to a container app or route in environment '<env>'
```

The **first** bind of any vanity hostname cannot converge in a single provision, and this
is a property of Container Apps, not a bug in the template. Azure will not issue a managed
certificate for a hostname that is not already a custom hostname in the environment — but
`web.bicep` / `gateway.bicep` declare the certificate and the ingress binding together, and
ARM creates the certificate first because the app depends on `webCert.id`. The resource
that would introduce the hostname is blocked on the hostname already existing.

It fails ~20 minutes in, after the Foundry accounts, gateway, and data tier are built, so
it reads like a late infrastructure fault rather than an ordering precondition.

Fix — register the hostname once, then let Bicep do the rest:

```powershell
az containerapp hostname add -g rg-ai4ia-<env> -n ca-web-<env> --hostname <host>
azd provision
```

`hostname add` creates the hostname with `bindingType: Disabled` and no certificate, which
is exactly the precondition. `azd provision` then issues the certificate and flips it to
`SniEnabled`. Confirm both:

```powershell
az containerapp show -g rg-ai4ia-<env> -n ca-web-<env> `
  --query "properties.configuration.ingress.customDomains[].{name:name,binding:bindingType}"
```

**The GitHub Actions deploy already handles this** — the *Preflight custom-domain bindings*
step registers any unregistered hostname before `azd provision`, so setting the repo
variables and re-running is sufficient. You only do it by hand when provisioning locally.

If `hostname add` itself fails, the problem is DNS, not ordering: that command is where
Azure validates domain control. It needs `CNAME <host>` → the app's
`*.azurecontainerapps.io` FQDN **and** `TXT asuid.<host>` = the app's
`customDomainVerificationId`, both visible to a public resolver. Check against one
directly — a corporate resolver can cache a stale target for the full TTL and show you the
*old* environment long after the record is correct:

```powershell
$ns = (Resolve-DnsName <zone> -Type NS -DnsOnly).NameHost
Resolve-DnsName <host> -Type CNAME -Server (Resolve-DnsName $ns[0] -Type A).IPAddress
```

Full cutover sequence, including where this sits: §3 step 3a.

### 7.13 "This browser can't play the returned audio format" (Speak / TTS)

The browser reports `MEDIA_ERR_SRC_NOT_SUPPORTED` even though the response carries a
valid `audio/*` `Content-Type`, so every content-type check between Foundry and the
`<audio>` element passes and nothing logs an error.

**Cause.** The APIM outbound policy sets the `TOKENPROCESSOR` response header, which
tells SimpleL7Proxy to stream the body through `JsonStreamProcessor` to extract token
usage. That processor reads the stream with a `StreamReader` and re-emits it with
`WriteLineAsync`, which is **lossy for anything that is not text**: bytes that are not
valid UTF-8 become U+FFFD (`ef bf bd`) and raw `0x0D`/`0x0A` bytes are consumed as line
terminators and rewritten as the platform newline. Applied to an mp3 this produces UTF-8
mojibake of roughly double the original size while the `Content-Type` survives intact.

The policy now gates that header on a text content type (`application/json` or `text/*`),
so binary bodies fall through to the proxy's byte-clean pass-through processor. The API
additionally sniffs the container signature before labelling the response and returns
`502 ... is not valid <fmt> audio` rather than serving a payload the browser can only
reject opaquely.

**Confirm which hop is at fault** by comparing the first bytes from APIM directly against
the same call through the proxy. mp3 must start `ID3` or `ff fx`; `ef bf bd` is the
corruption signature:

```powershell
# through the proxy (the path the API uses)
$dep = "gpt-4o-mini-tts-<env>-<region>-glbl"
$h = @{ 'S7P-KEY' = $proxyKey; 'Content-Type' = 'application/json' }
$b  = @{ input='Hi.'; voice='alloy'; response_format='mp3'; model=$dep } | ConvertTo-Json
Invoke-WebRequest -Method POST -Headers $h -Body $b -OutFile out.bin `
  -Uri "https://<proxy-host>/openai/deployments/$dep/audio/speech?api-version=2025-03-01-preview"
'{0:x2} {1:x2} {2:x2}' -f [IO.File]::ReadAllBytes('out.bin')[0..2]
```

Swap the host for `https://<apim>.azure-api.net` with `Ocp-Apim-Subscription-Key` and an
`x-LLMModel` header to isolate APIM. If APIM is clean and the proxy is not, the
`TOKENPROCESSOR` guard has regressed — see
`test_tokenprocessor_is_only_set_for_text_response_bodies`.

Anything else routed through the proxy that returns a binary body (image or video bytes,
file downloads) fails the same way, so fix the header rather than special-casing audio.

### 7.14 A malformed request returns `429 Requeue Message` with an empty body

The caller sends something Foundry genuinely rejects — an unsupported
`reasoning_effort` value, a bad parameter for the model — and instead of the `400`
explaining what is wrong, the client gets `429`, an empty body, `retry-after-ms:
9999`, and (through the proxy) eventually `No active hosts were able to handle the
request`. The request is not a capacity problem and no amount of retrying will make
it succeed, so the status code sends every layer above it down the wrong path.

**Cause.** APIM's outbound classifier treated *every* `400` as a temporary error.
The intent was narrow: an Azure OpenAI `context_length_exceeded` 400 really does
depend on which backend was chosen (a PTU deployment with a smaller window), and
the retry loop deliberately skips PTU backends once `contextWindowExceeded` is set.
That single legitimate case was implemented by widening the classifier to all 400s.

Three harms compound from the one misclassification:

1. The request is retried twice upstream even though it is deterministic.
2. `isTempError` also gates the throttle block, so a malformed request **parks a
   healthy backend for 10 seconds for every other caller in that region**. One
   client looping on a bad parameter degrades the whole deployment.
3. `Return429` replaces the response with `429 Requeue Message` and an empty body,
   destroying the provider's own diagnostic. The proxy then correctly honours the
   requeue headers and retries across backends, multiplying the cost.

This is the same bug class as §7.10 (a `401` laundered into a retryable `429` in
the *authorization* stage); this one is in the *backend-response* stage.

**Fix (already in the policy).** `isRetryableBadRequest` parses the 400 body once
for `error.code == "context_length_exceeded"`; `isTempError` gates its 400 disjunct
on that variable; `contextWindowExceeded` reuses the same decision instead of
re-parsing; and the throttle gate no longer lists a bare `400`. `isPermError`'s
lower bound moved from `> 400` to `>= 400` at the same time — **this coupling is
load-bearing.** A 400 excluded from `isTempError` but not admitted to `isPermError`
would be *neither*, which leaves `RetryRemaining` true and reinstates the exact
retry loop the change removes.

**Confirm the fix is live** by sending a value the model rejects and checking that
the real error survives, then that the backend was not parked:

```powershell
$dep  = "gpt-5.6-sol-<env>-<region>-glbl"
$h    = @{ 'S7P-KEY' = $proxyKey; 'Content-Type' = 'application/json' }
# NOTE: do not send x-LLMModel through the proxy - it derives and adds it itself
# from the path, and a second copy returns 400 model_path_mismatch (see 7.11).
$body = @{ messages=@(@{ role='user'; content='hi' }); reasoning_effort='minimal' } | ConvertTo-Json -Depth 5
try {
  Invoke-WebRequest -Method POST -Headers $h -Body $body `
    -Uri "https://<proxy-host>/openai/deployments/$dep/chat/completions?api-version=2025-04-01-preview"
} catch {
  $_.Exception.Response.StatusCode.value__       # expect 400, NOT 429
  (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())).ReadToEnd()
}
```

Expect `400` with a body naming the unsupported value. A `429` with an empty body
means the classification has regressed — see
`test_a_malformed_400_is_a_permanent_error`. Immediately re-send a *valid* request
to the same deployment: it must return `200`. If it returns `429` for ~10s, the
throttle gate is still admitting plain 400s.

**Side effect worth knowing:** while this bug was live, the system could not be
probed for which parameter values a model accepts, because each rejection threw the
backend into a throttle that made the *next* probe report a false `429`. Any such
probe needed 11+ seconds of spacing between attempts.

### 7.15 A terminal 4xx returns the right status with `Content-Length: 0`

Fixing §7.14's classification exposed the second half of the same problem. Verified
live immediately after that deploy: the malformed request returned

```
HTTP/1.1 400 Bad Request
backendlog: ... 0.080s StatusCode: 400 - Perm Error
content-length: 0
```

Correct status, correct classification, `THROTTLED: (none)`, one attempt, no
`S7PREQUEUE` — and still no way to tell *what* was wrong.

**Cause.** APIM's `<return-response>` **replaces the response wholesale**. The
permanent-error branch set a status and three diagnostic headers but had no
`<set-body>`, so the provider's body was silently discarded. This applied to every
terminal 4xx from Foundry, not just the malformed-parameter case: an unsupported
value, an unknown deployment, a content-filter block, an expired data-plane
credential — all arrived as a bare status. The reason only ever lives in the body,
so the effect was that no client-side error was actionable without reproducing the
call outside the gateway.

**Fix (already in the policy).** The permanent-error `<return-response>` now
re-emits the upstream body and its `Content-Type`, falling back to the previous
synthetic `{statusCode, statusReason}` JSON only when the upstream body is empty or
unreadable. The body is read with `preserveContent: true` — the same read
`isRetryableBadRequest` already performs — so no extra buffering is introduced.
Pinned by `test_a_permanent_error_returns_the_upstream_body`.

**Confirm.** Re-run §7.14's request and check `Content-Length` is non-zero and the
body names the offending value. If the status is right but the body is empty, a
`<set-body>` has been dropped from the permanent-error branch.

## APIM Basic v2 migration guardrail

The active model/realtime/MCP plane is the `apim-mcp-<workload>-<environmentName>-<uniqueSuffix>` Basic v2 service (capacity 1). `apimcore.bicep` owns its identity and single diagnostic setting; `mcpgateway.bicep` preserves the MCP children and `gateway.bicep` adds model/realtime children through the shared contract, including the additive `speech_voice_live` WebSocket API (`/speech/voice-live/realtime`) and its own distinct subscription (`ai4ia-api-speech-voice-live`) alongside the existing `/openai/realtime` API and subscription. Reusing this paid service adds no roughly $150 APIM base cost, but MCP, HTTP/SSE, and both voice providers share its blast radius and resilience posture. The Consumption APIM and all children remain unchanged/inactive rollback with no active traffic — it never receives Speech Voice Live traffic either. MCP uses an MCP-only product/subscription, so its key cannot call model/realtime APIs; equally, neither voice provider's key can call the other's API, the model API, or the MCP plane. Configure APIs, policies, keys, and Foundry RBAC before caller revisions update. Review a zero-delete what-if; delete Consumption only in a separately approved post-stabilization change.

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
