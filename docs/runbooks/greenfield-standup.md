# Runbook: Greenfield Azure standup

> Start here to deploy AI4IA into a new Azure subscription or Microsoft Entra
> tenant. This guide owns the one-time control-plane and identity work. After the
> first validated release, use the
> [routine deployment runbook](./deployment.md).

AI4IA is an Azure capability showcase with production-oriented governance
controls, not a turnkey production service. A clean standup still requires
tenant-specific identity, quota, consent, DNS, and operational decisions. Do not
treat a successful `azd provision` as proof that sign-in, model routing, or every
optional capability works.

## Standup sequence

1. Preflight the subscription, providers, model access, lifecycle, and quota.
2. Create one deployment identity and two GitHub OIDC federated credentials.
3. Grant Azure RBAC and configure GitHub variables/secrets.
4. Create the API and SPA Entra registrations.
5. Select the environment posture and naming tokens.
6. Provision once without custom domains, then bind domains in a second phase.
7. Provision data-plane assets that Bicep cannot create.
8. Validate the first release and, for migrations, move tenant-specific state.

## 1. Preflight the target

Use an account authorized to register resource providers and create role
assignments:

```powershell
az login
az account set --subscription <subscription-id>
az account show --query "{tenant:tenantId,subscription:id,name:name}" -o table

python scripts/check-resource-providers.py
python scripts/check-model-availability.py
```

An untouched subscription can have most required providers unregistered. The
provider script derives namespaces from `infra/**/*.bicep`, so it stays aligned
with the template:

```powershell
python scripts/check-resource-providers.py --register
```

Registration is asynchronous and can take several minutes. Wait for every
required namespace to report `Registered` before provisioning.

`azure.yaml` also runs the full model check (including quota) as a non-optional
`preprovision` hook on Windows and POSIX. The deploy workflow has already logged
Azure CLI into `AZURE_SUBSCRIPTION_ID`; a local `azd provision` must have a matching
`az login` / `az account set` context or it fails before ARM receives the template.
The commands above remain the fastest way to diagnose or remediate findings early.
When no target resource group/environment exists, the script says it is in
**greenfield/addition mode** and treats every desired deployment as new. On a
routine reconcile it inventories only this environment's Foundry accounts. A
`Deprecating`/`Deprecated` record is allowed only when the exact named deployment
is already `Succeeded` with the desired model, version, SKU, capacity, and version
upgrade posture; that path emits a migration warning. This exact-existing exception
also covers a retired model/SKU disappearing from offer data or quota being reduced
below already-provisioned capacity. Any absent/drifted member in the same model/SKU
group restores full enforcement over the total desired capacity.

The model preflight compares `infra/models.json` with the subscription on three
independent axes:

- **Availability:** limited-access models need approval; partner models can need
  a Marketplace offer.
- **Lifecycle:** `Deprecating` or `Deprecated` versions can keep serving an
  existing deployment while refusing a new one.
- **Quota:** requested capacity is summed by the scope Azure actually enforces.
  Non-OpenAI publisher quota can be subscription-wide even though the usage API
  repeats the same counter under every region.

Quota counter names do not reliably match catalog model names. The script
reconciles known publisher prefixes and spellings; an unmatched counter remains
a warning because absence is ambiguous. Confirm it manually:

```powershell
az cognitiveservices usage list --location <region> -o table
```

If a model is unavailable, request access or remove it from `infra/models.json`.
If it lacks quota, request an increase or lower capacity. After any catalog edit:

```powershell
python scripts/gen-model-catalog.py
python scripts/gen-model-catalog.py --check
python scripts/gen-gateway-policy.py
python scripts/gen-gateway-policy.py --check
python scripts/validate-catalog.py
```

Run preflight before the first provision. These failures otherwise appear after
the resource group, Foundry accounts, gateway, and data tier have already been
created.

## 2. Create the deployment identity and OIDC trust

Create a user-assigned managed identity in the target tenant:

```powershell
$subscriptionId = az account show --query id -o tsv

az group create -n rg-ai4ia-cicd -l eastus2
az identity create -g rg-ai4ia-cicd -n id-ai4ia-deploy --location eastus2

$clientId = az identity show -g rg-ai4ia-cicd -n id-ai4ia-deploy --query clientId -o tsv
$principalId = az identity show -g rg-ai4ia-cicd -n id-ai4ia-deploy --query principalId -o tsv
```

Create **both** federated credentials:

```powershell
# deploy.yml uses environment: production, so its OIDC subject is environment-scoped.
az identity federated-credential create `
  --identity-name id-ai4ia-deploy -g rg-ai4ia-cicd `
  --name github-ai4ia-production `
  --issuer https://token.actions.githubusercontent.com `
  --subject "repo:ian-t-adams/AI4IA:environment:production" `
  --audiences api://AzureADTokenExchange

# pages.yml has no environment on its build job; live status refresh uses main's ref subject.
az identity federated-credential create `
  --identity-name id-ai4ia-deploy -g rg-ai4ia-cicd `
  --name github-ai4ia-main `
  --issuer https://token.actions.githubusercontent.com `
  --subject "repo:ian-t-adams/AI4IA:ref:refs/heads/main" `
  --audiences api://AzureADTokenExchange
```

Both subjects are required on the same deployment identity. The
production-environment subject authorizes `deploy.yml`; the main-branch subject
separately authorizes the Azure-backed status refresh in
`.github/workflows/pages.yml`. Do not create a second Azure identity for Pages.
Adding an environment to the Pages build job would change its subject and break
this trust. Missing branch federation makes Azure login fail, and the workflow
intentionally refuses to publish stale seed data.

Grant the deployment identity the roles needed for the first subscription-level
provision:

```powershell
az role assignment create `
  --assignee-object-id $principalId `
  --assignee-principal-type ServicePrincipal `
  --role Contributor `
  --scope "/subscriptions/$subscriptionId"

az role assignment create `
  --assignee-object-id $principalId `
  --assignee-principal-type ServicePrincipal `
  --role "Role Based Access Control Administrator" `
  --scope "/subscriptions/$subscriptionId"
```

Use the **principal/object id**, never the client id, with
`--assignee-object-id`. Azure can accept the client id and create a role row that
grants the identity nothing. Verify assignments by subscription/resource scope
and inspect the literal `principalId`; `az role assignment list --assignee`
resolves identifiers and can hide this mistake.

Retain both roles at subscription scope for routine provisioning. This is the
tested permission boundary, not merely first-run bootstrap:

- `infra/main.bicep` has `targetScope = 'subscription'`, creates the resource
  group, and submits subscription-scoped role assignments.
- The deploy preflight discovers and registers resource providers at subscription
  scope whenever the template adds a namespace.
- Bicep continues assigning application data-plane roles during later
  reconciliations.

Do **not** narrow `Contributor` to the resource group: that makes a routine
`azd provision` unable to execute the same subscription-scoped deployment. A
future least-privilege replacement must be a tested custom role that covers the
template's subscription deployment, resource-group lifecycle, provider
registration, and role-assignment operations; no such role is maintained here
today.

The deploy workflow resolves the identity's principal id from
`AZURE_CLIENT_ID`, exports azd's `AZURE_PRINCIPAL_ID`, and grants the deployment
identity **Cognitive Services Content Understanding Contributor** on the primary
Foundry account. When the Foundry toolbox is enabled, Bicep also grants this
provisioning identity project-scoped **Foundry User** so the foundry-assets
workflow can reconcile canonical assets. When the private catalog is enabled,
it receives only **Azure API Center Data Reader** at that API Center service
scope. These are provisioning grants, not app runtime access.

## 3. Configure GitHub

### 3.1 Repository and environment variables

Set repository variables under **Settings → Secrets and variables → Actions**:

| Variable | Required value |
|---|---|
| `AI4IA_DEPLOYMENT_ENABLED` | `true`; use `false` only when the repository intentionally has no Azure deployment target |
| `AZURE_CLIENT_ID` | Deployment identity client id |
| `AZURE_TENANT_ID` | Target Entra tenant GUID |
| `AZURE_SUBSCRIPTION_ID` | Target subscription GUID |
| `AZURE_ENV_NAME` | Short azd environment token such as `prod` |
| `AZURE_LOCATION` | Primary Azure region such as `eastus2` |
| `AI4IA_APP_ENVIRONMENT` | `prod` for a real deployment |
| `AI4IA_AUTH_PROVIDER` | `entra` for a real deployment |
| `AI4IA_OWNER` | Accountable owner tag |
| `AI4IA_COST_CENTER` | Chargeback/cost-center tag |
| `AI4IA_APIM_PUBLISHER_EMAIL` | Operator-owned service mailbox |
| `AI4IA_BUDGET_START_DATE` | Fixed `yyyy-MM-01` month to keep budget deployment idempotent |

Unset `AI4IA_DEPLOYMENT_ENABLED` is treated as enabled for compatibility, but
the five `AZURE_*` identity/environment variables above are still required.
Only the explicit value `false` allows the deploy job to skip successfully.

`AI4IA_ALLOW_DEV_AUTH` must remain false. A production posture ignores it, but
keeping the value false also prevents a non-production deployment from trusting
caller-supplied `X-Dev-User` identity.

After creating the Entra registrations in section 4, also set:

| Variable | Value |
|---|---|
| `AI4IA_ENTRA_TENANT_ID` | Target tenant GUID |
| `AI4IA_ENTRA_AUDIENCE` | API application client id |
| `AI4IA_ENTRA_API_SCOPE` | `api://<api-client-id>/access_as_user` |
| `AI4IA_ENTRA_WEB_CLIENT_ID` | SPA application client id |
| `AI4IA_ADMIN_SUBJECTS` | Comma-separated Entra user object ids (`oid`) allowed to use admin APIs |

An admin user's object id is tenant-specific. Read it while signed into the
target tenant:

```powershell
az ad signed-in-user show --query id -o tsv
```

Set feature/capacity variables only after reading
[`feature-enablement.md`](./feature-enablement.md) and
[`../configuration-reference.md`](../configuration-reference.md). A repository
variable does nothing unless deploy.yml exports it and the parameter file
consumes it; the CI reachability test enforces the current mapping.

### 3.2 Secrets

Web IQ can use either an entitled managed identity or an API key. To use a key,
store it as a secret on the `production` GitHub environment:

```powershell
gh secret set AI4IA_WEBIQ_API_KEY --env production
```

When the secret is empty, Bicep removes the Container App secret and the API
falls back to managed identity. That identity must be entitled for
`https://api.microsoft.ai/.default`, or every live-search call returns 401.
The admin Web search health panel reports `api_key`, `managed_identity`, or
`unconfigured` and categorizes recent failures.

Any profile-projection JSON is also secret and must not be a repository variable.
Proxy application profiles remain blocked while the public edge uses shared-key
identity.

### 3.3 Protect the production environment and enable Pages

Under **Settings → Environments → production**, restrict deployment branches to
`main` and add reviewers if the organization requires an approval gate.
Referencing the environment in the workflow creates it but does not add
protection automatically.

Under **Settings → Pages**, select **GitHub Actions** as the source. The Pages
build uses the main-branch federated credential from section 2 to refresh live
status before publishing. Its repository-variable check, Azure login, and status
refresh are mandatory: any failure stops artifact upload rather than republishing
an old snapshot.

## 4. Create the application Entra registrations

The deployment identity provisions Azure resources. User sign-in requires two
separate tenant objects that Bicep cannot create: an API registration and a web
SPA registration.

The first provision has no deployed web FQDN yet, so bootstrap the registrations
with the local redirect first. This creates the API audience, scope, SPA client,
and service principals needed by the deployment and its app-only canary:

```powershell
# Inspect planned changes.
./scripts/provision-entra-apps.ps1 `
  -WebRedirectUri http://localhost:3000

# Create/update the apps and print the repository-variable values.
./scripts/provision-entra-apps.ps1 `
  -WebRedirectUri http://localhost:3000 `
  -AdminUpn <admin-upn> `
  -Apply
```

Set the printed `AI4IA_ENTRA_*` repository variables before the first provision.
Browser sign-in on Azure is not ready yet; section 6.1 discovers the default ACA
web FQDN and reruns this idempotent script to add that real origin. Section 6.2
runs it once more if a vanity web hostname is bound. Existing redirects are
retained, so each pass adds the newly available origin without breaking local or
default-host access.

The script creates:

1. An API app with v2 access tokens and delegated scope
   `api://<api-client-id>/access_as_user`.
2. A single-tenant web SPA app with the deployed web origin and local development
   redirect, delegated access to the API scope, and no client secret.
3. Service principals needed for sign-in and app-only deploy canary token
   acquisition.

The web app must use an SPA redirect URI. `az ad app create` has no SPA redirect
flag; the script uses Microsoft Graph rather than misclassifying it as a web or
public-client redirect.

### Admin consent

Admin consent is optional when both conditions hold:

1. `access_as_user` has scope type `User`.
2. The tenant's default user role has a
   `ManagePermissionGrantsForSelf.*` permission-grant policy.

Check both:

```powershell
az ad app show --id <api-client-id> `
  --query "api.oauth2PermissionScopes[].{value:value,whoCanConsent:type}"

az rest --method GET `
  --url "https://graph.microsoft.com/v1.0/policies/authorizationPolicy" `
  --query "defaultUserRolePermissions.permissionGrantPoliciesAssigned"
```

When user consent is allowed, skipping admin consent produces a one-time
per-user prompt for the single AI4IA API scope. Granting admin consent later
removes that prompt; it is not otherwise a deployment prerequisite.

If tenant policy disables user consent, an administrator must consent.
Subscription Owner is not sufficient because Azure RBAC and Entra directory
roles are separate planes. Consent requires an appropriate directory role such
as Privileged Role Administrator, Cloud Application Administrator, or Global
Administrator.

Who may sign in is separate from consent. A new service principal defaults to
`appRoleAssignmentRequired=false`, allowing any tenant user once consent exists.
To restrict sign-in, enable assignment required and assign users/groups:

```powershell
az ad sp update --id <web-client-id> --set appRoleAssignmentRequired=true
```

AI4IA admin access is not an Entra app role. The API gates admin routes with the
tenant-specific `AI4IA_ADMIN_SUBJECTS` object-id allowlist.

## 5. Select environment posture and names

Environment-specific values are centralized:

| Concern | Source | Portability note |
|---|---|---|
| Resource environment | `AZURE_ENV_NAME` | Feeds the resource group and most resource names |
| Subscription/tenant/region | `AZURE_*` repository variables | Must target the new environment |
| Deployment identity | `AZURE_CLIENT_ID` | Recreate in every tenant |
| User authentication | `AI4IA_ENTRA_*` | Recreate registrations in every tenant |
| Admin identities | `AI4IA_ADMIN_SUBJECTS` | User object ids change across tenants |
| Model deployment token | `infra/models.json` `naming.subscriptionToken` | Stamped into deployment names and runtime catalog |
| Foundry token | `infra/models.json` `naming.foundryToken` | Names Foundry accounts/projects and toolbox endpoint |
| APIM publisher | `AI4IA_APIM_PUBLISHER_EMAIL` | Use a monitored mailbox |
| Ownership tags | `AI4IA_OWNER`, `AI4IA_COST_CENTER` | Replace repository defaults |

Globally unique resource names additionally carry
`uniqueString(subscription().id, environmentName)`, so a new subscription gets
independent APIM, Foundry, Key Vault, Cosmos, Search, and ACR names.

If either catalog naming token changes, regenerate before provisioning:

```powershell
python scripts/gen-model-catalog.py
python scripts/gen-gateway-policy.py
python scripts/validate-catalog.py
```

Do not edit deployment names in application code. The catalog is the single
source of truth.

## 6. Provision in two phases

### 6.1 First provision without custom domains

For a new environment, leave these four values empty:

- `AI4IA_WEB_CUSTOM_DOMAIN`
- `AI4IA_WEB_MANAGED_CERT_NAME`
- `AI4IA_PROXY_CUSTOM_DOMAIN`
- `AI4IA_PROXY_MANAGED_CERT_NAME`

The first standup must use the GitHub deployment workflow from `main`:

```powershell
gh workflow run deploy.yml -f provision=true --ref main
```

This is required, not merely preferred. The workflow resolves the deployment
identity's **principal/object id** from `AZURE_CLIENT_ID` (never treating the
client id as an object id), exports it as azd's native
`AZURE_PRINCIPAL_ID`, and Bicep grants that service principal narrow roles:
**App Configuration Data Owner** on the configuration store and
**Cognitive Services Content Understanding Contributor** on the primary Foundry
account. The postprovision hook runs under the same OIDC identity, reconciles the label-aware
`Warm:Sentinel` with Entra authentication, and PATCHes the Content Understanding
model defaults. The proxy retains only App Configuration Data Reader; web and API
receive no App Configuration data role.

A local human operator is neither the API managed identity nor that service
principal. The current role-assignment template declares
`principalType: 'ServicePrincipal'`, so putting a signed-in user's object id into
`AZURE_PRINCIPAL_ID` is not a supported workaround. Enabled Content Understanding
now fails the provision when any explicit primary output is absent, no Cognitive
Services token can be acquired, or the defaults PATCH exhausts its retries. A local
`azd up` therefore cannot downgrade an incomplete CU setup to `WARN`, but it remains
unsupported for this greenfield identity path. The workflow also captures rollback
state, promotes images by digest, and verifies the resulting release.
On the first standup there are no prior revisions to restore, so a failed first
release must be corrected and rerun.

The default Container Apps hostnames are reachable immediately, but browser
sign-in is not valid there until the web origin is added to the SPA registration.
Record:

```powershell
$web = az containerapp show -g rg-ai4ia-<environment> -n ca-web-<environment> `
  --query "{fqdn:properties.configuration.ingress.fqdn,verificationId:properties.customDomainVerificationId}" |
  ConvertFrom-Json
$web

az containerapp show -g rg-ai4ia-<environment> -n ca-proxy-<environment> `
  --query "{fqdn:properties.configuration.ingress.fqdn,verificationId:properties.customDomainVerificationId}"
```

Now add the deployed web origin to the existing SPA registration:

```powershell
./scripts/provision-entra-apps.ps1 `
  -WebRedirectUri "https://$($web.fqdn)",http://localhost:3000 `
  -AdminUpn <admin-upn> `
  -Apply
```

This post-provision rerun is required even when no custom domain will be used.
Confirm browser sign-in on `https://$($web.fqdn)` before moving to optional DNS.

### 6.2 Bind custom domains (optional)

The first managed-certificate bind cannot converge in one ARM pass. Azure
requires the hostname to exist in the Container Apps environment before it
issues a certificate, while Bicep declares the certificate and ingress binding
together and ARM creates the certificate first. Reversing the sequence fails
with `RequireCustomHostnameInEnvironment`.

Use this order:

1. Provision with all four domain variables empty.
2. At the public DNS provider, point each `CNAME` to the new Container App FQDN
   and set `TXT asuid.<host>` to that app's verification id.
3. Wait for the old TTL and verify through a public resolver.
4. Add the vanity web origin to the existing Entra SPA registration:

   ```powershell
   ./scripts/provision-entra-apps.ps1 `
     -WebRedirectUri https://<web-host>,"https://$($web.fqdn)",http://localhost:3000 `
     -AdminUpn <admin-upn> `
     -Apply
   ```

5. Set `AI4IA_WEB_CUSTOM_DOMAIN` and `AI4IA_PROXY_CUSTOM_DOMAIN`. Leave managed
   certificate names empty for this first bind so Bicep derives new names.
6. Rerun the GitHub deployment with provision enabled.

The workflow's **Preflight custom-domain bindings** step runs
`az containerapp hostname add` before provision when the hostname is new. For a
local provision, perform that phase manually:

```powershell
az containerapp hostname add `
  -g rg-ai4ia-<environment> `
  -n ca-web-<environment> `
  --hostname <web-host>

az containerapp hostname add `
  -g rg-ai4ia-<environment> `
  -n ca-proxy-<environment> `
  --hostname <proxy-host>

azd provision
```

`hostname add` creates a disabled binding with no certificate and validates
domain control. Provision then issues the certificate and changes the binding
to `SniEnabled`.

```powershell
az containerapp show -g rg-ai4ia-<environment> -n ca-web-<environment> `
  --query "properties.configuration.ingress.customDomains[].{name:name,binding:bindingType}"
```

Once issued, record the actual managed certificate names in
`AI4IA_WEB_MANAGED_CERT_NAME` and `AI4IA_PROXY_MANAGED_CERT_NAME`. Every later
provision must include all four values; omitting a hostname removes its binding.

Never copy certificate names from another tenant. Managed certificates belong
to that tenant's Container Apps environment.

## 7. Complete data-plane setup and first validation

When the checked-in `enableFoundryToolbox=true` parameter is deployed, Bicep
creates the control-plane wiring but not the Foundry data-plane toolbox:

```powershell
# Run from the repository root. This exact extra is provisioning-only.
python -m pip install -e "app/api[foundry]"

# Derive the endpoint from the selected azd environment; do not construct it.
$projectEndpoint = azd env get-value AZURE_FOUNDRY_PROJECT_ENDPOINT
if (-not $projectEndpoint) { throw "AZURE_FOUNDRY_PROJECT_ENDPOINT is empty" }
$env:AZURE_FOUNDRY_PROJECT_ENDPOINT = $projectEndpoint

# Local access validation is diagnostic only; the workflow remains authoritative.
python scripts/provision-foundry-toolbox.py --check-access
```

The preflight fails closed unless the caller has project-scoped **Foundry User**.
For GitHub reconciliation, Bicep grants that role to the nonempty
`deploymentPrincipalId`; Azure OIDC login alone is not data-plane authorization.

Keep the established order: the deploy workflow provisions RBAC first, then the
Foundry-assets workflow reconciles the toolbox with the same OIDC identity. Set
the endpoint as the authoritative `production` environment variable. Do not keep
a same-named repository variable: GitHub makes environment-level configuration
available to the `vars` context only after the protected job starts; if the
environment value is absent, a repository value can become the lower-precedence
fallback. The check/cleanup below removes that ambiguity.

```powershell
$repoEndpoint = gh variable get AZURE_FOUNDRY_PROJECT_ENDPOINT 2>$null
if ($repoEndpoint -and $repoEndpoint -ne $projectEndpoint) {
  Write-Warning "Removing conflicting repository-scoped endpoint: $repoEndpoint"
}
if ($repoEndpoint) {
  gh variable delete AZURE_FOUNDRY_PROJECT_ENDPOINT
}
gh variable set AZURE_FOUNDRY_PROJECT_ENDPOINT --env production --body $projectEndpoint
$storedEndpoint = gh variable get AZURE_FOUNDRY_PROJECT_ENDPOINT --env production
if ($storedEndpoint -ne $projectEndpoint) {
  throw "production AZURE_FOUNDRY_PROJECT_ENDPOINT does not match selected azd environment"
}

$previousRunId = gh run list --workflow foundry-assets.yml --event workflow_dispatch `
  --branch main --limit 1 --json databaseId --jq '.[0].databaseId'
gh workflow run foundry-assets.yml --ref main
$deadline = (Get-Date).AddMinutes(2)
do {
  Start-Sleep -Seconds 3
  $runId = gh run list --workflow foundry-assets.yml --event workflow_dispatch `
    --branch main --limit 1 --json databaseId --jq '.[0].databaseId'
} while ((-not $runId -or $runId -eq $previousRunId) -and (Get-Date) -lt $deadline)
if (-not $runId -or $runId -eq $previousRunId) {
  throw "No new foundry-assets workflow_dispatch run appeared within two minutes."
}
gh run watch $runId --exit-status
```

Do not replace this with a local `--create` for first standup evidence; a local
operator and the GitHub deployment identity can have different project roles.
The workflow installs the same `app/api[foundry]` extra, checks access, and then
reconciles the canonical toolbox.

Skipping this can leave APIM and the MCP `initialize` handshake healthy while
`tools/list` returns `Toolbox '<name>' not found`. As an authenticated admin,
request `/api/admin/metrics/official-mcp?refresh=true`; that endpoint performs
the full `initialize` -> `tools/list` flow. Require the `ai4ia-toolbox` server to
report `toolCount: 3` and `lastError: null`. A ping or `initialize` alone is not
acceptance.

In the workflow's **Provision infrastructure** log, require both postprovision
results:

- `App Configuration sentinel | PASS`
- `Content Understanding defaults | PASS`

When Content Understanding is enabled, a missing primary output or token and a
PATCH that still fails after the bounded RBAC-propagation retries is recorded as
`FAIL` and fails the provision (`continueOnError: false`). `SKIP` is valid only when
the Bicep output explicitly says Content Understanding is disabled. After correcting
the reported cause or allowing RBAC propagation, rerun:

```powershell
gh workflow run deploy.yml -f provision=true --ref main
```

Do not continue until the rerun reports `PASS`; the workflow cannot proceed past
an enabled Content Understanding defaults failure because document upload would
otherwise reach the live account without model defaults.

Run the first-release checks. This is an **acceptance procedure**, not evidence
that any check has run. Record timestamp, operator, revision/digest, and result
for every row; keep failures as failures rather than converting them to notes.

1. When deployed through GitHub, confirm all three Container Apps serve the exact
   digests in the deploy job summary and are active, healthy, and running. A
   local `azd up` does not produce this evidence.
2. Confirm API `/health/live` and `/health/ready`, web `/`, and proxy ingress.
3. Confirm every configured custom domain is `SniEnabled`.
4. Sign in through the SPA at the default ACA FQDN and, when configured, the
   vanity hostname; perform one model-backed chat.
5. Verify the authenticated API path reaches SimpleL7Proxy → APIM → Foundry.
6. Exercise each enabled capability with the bounded fixtures below; the
   deployment canary proves only one non-streaming chat turn.
   For Voice Live, derive `wss://.../api/voice/live` from the direct
   `AZURE_API_URL`; never use the web/Next.js hostname, which cannot proxy
   WebSockets. A fresh standup keeps Speech Voice Live off unless both
   `AI4IA_SPEECH_VOICE_LIVE_ENABLED=true` and
   `AI4IA_VOICE_PROVIDER_ALLOWLIST=azure_openai,speech_voice_live` are supplied.
   The current production override and its 2026-08-08 successful canaries do not
   change that template default.
7. Confirm the expected admin user can reach `/api/admin/*`.
8. Confirm Web search health reports the intended auth mode.
9. If Pages is enabled, run/publish it and confirm the status snapshot names the
   new subscription, resource group, and endpoints.

| Capability | Bounded fixture | Expected result | Cleanup |
| --- | --- | --- | --- |
| Chat/gateway | New session; prompt `Reply with exactly AI4IA-FIRST-RELEASE-OK.` once | One assistant reply with the sentinel; persisted session reloads; gateway correlation appears once | Delete the test session |
| Document ingest/retrieval | Upload one UTF-8 text file under 1 KiB containing a unique UUID; ask for that UUID | Manifest reaches `ready`; retrieval returns the exact UUID from the owned document | Delete the library document and confirm it no longer retrieves |
| Memory | Save `acceptance-color-<UUID> is blue`, query it once, then issue the supported forget action | Recall returns the exact UUID/value before forget and does not return it after | Confirm forget succeeded; delete the test session |
| Official MCP | Admin request to `/api/admin/metrics/official-mcp?refresh=true` | Full `initialize` -> `tools/list`; `ai4ia-toolbox.toolCount == 3`; `lastError == null` | None; read-only |
| BYO MCP (when enabled) | Register a disposable HTTPS MCP test server exposing one no-op tool; invoke it once in interactive chat | Discovery exposes one namespaced tool; the invocation is held for exact-argument approval before dispatch | Delete the MCP registration and its Key Vault secret |
| Web search (when enabled) | One query for a stable public fact with a source requirement | One bounded result set with source links; invocation approval follows current policy | Delete the test session |
| Image (when enabled) | One lowest-supported-size image: `solid blue square, no text` | One completed artifact owned by the operator; usage entry records the attempt | Delete the generated artifact |
| Video (when enabled) | One shortest-supported-duration clip: `solid blue frame, no text or audio` | One completed artifact owned by the operator; an explicit provider failure is a failed acceptance result | Delete the generated artifact |
| Azure OpenAI Voice Live | One session capped at 30 seconds; say `first release voice check`; stop immediately after transcript | Authenticated WebSocket succeeds and transcript contains the phrase | End and delete the test session; retain no audio |
| Speech Voice Live (when enabled) | Repeat the same 30-second fixture with provider explicitly set to `speech_voice_live` | Provider switch is visible; authenticated WebSocket succeeds; transcript contains the phrase | End and delete the test session; retain no audio |
| Admin | Open one usage rollup and one resource panel as the configured admin; repeat as a non-admin | Admin succeeds; non-admin receives the expected denial | None; read-only |

Use one UUID per run so cleanup can be proved with a negative lookup. Stop after
the first unexpected paid-operation result; do not fan out image/video/voice
retries while diagnosing. The table deliberately has no checked boxes because
the repository cannot truthfully claim live acceptance from static CI.

For the published portal:

```powershell
azd env select <environment>
./scripts/status-snapshot.ps1
```

The checked-in status/inventory snapshots otherwise continue describing the old
environment.

## 8. Migrate an existing deployment

A tenant migration is a parallel standup, not an in-place retarget:

1. Complete sections 1-5 in the new tenant.
2. Keep the old environment serving.
3. Provision the new environment without custom domains.
4. Add the new default ACA web FQDN to the Entra SPA registration and validate
   sign-in before changing DNS.
5. Recreate any required data-plane toolbox/catalog assets.
6. Decide what canonical data must move. Cosmos is user-scoped canonical storage;
   blob source files and per-user Key Vault secrets need separate handling.
7. Add the vanity origin to the SPA registration, move DNS, and complete the
   two-phase domain bind.
8. Regenerate published status/inventory data.
9. Retire the old environment only through the
   [teardown runbook](./teardown.md), with its explicit data-loss gate.

Do not carry these values across unchanged:

- The deployment identity and both federated credentials.
- API/SPA Entra application ids and service principals.
- `AI4IA_ADMIN_SUBJECTS`; the same person has a different object id in the new
  tenant.
- Managed certificate names.
- Subscription/resource ids or Foundry endpoints.

Memory is already Cosmos-backed in a greenfield environment. The
[memory migration runbook](./memory-migration.md) documents the retired
legacy memory-store transition and is not a prerequisite for a new tenant.

Operator scripts such as `inventory.ps1`,
`capture-data-recovery-state.ps1`, `teardown.ps1`,
`purge-soft-deleted.ps1`, and `seed-models.ps1` require explicit targets. Keep
using those target arguments during migration; never rely on an old local Azure
context.

## First-standup failures

Use the stable troubleshooting entries in the
[routine deployment runbook](./deployment.md#7-troubleshooting):

- [Budget start date cannot be updated](./deployment.md#72-provision-infrastructure-fails-with-start-date-of-budgets-cannot-be-updated)
- [Cosmos vector capability propagation](./deployment.md#74-provision-infrastructure-fails-cosmos-container-vector-policy--capability-has-not-been-enabled)
- [Orphaned active ARM deployment](./deployment.md#75-every-deploy-fails-validation-with-deploymentactive-cancelledtimed-out-provision)
- [Globally unique Azure name collision](./deployment.md#76-servicealreadyexists--the-name--is-already-taken-apim-api-center-foundry)
- [Deprecating model version](./deployment.md#78-servicemodeldeprecating-on-a-model-deployment)
- [Insufficient model quota](./deployment.md#79-insufficientquota-on-a-model-deployment)
- [First custom-hostname bind](./deployment.md#712-requirecustomhostnameinenvironment-on-a-managed-certificate)

Do not repeatedly rerun deterministic availability, lifecycle, or quota failures.
Correct the catalog or entitlement first, regenerate, and then reprovision.
