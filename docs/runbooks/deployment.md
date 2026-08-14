# Runbook: Routine deployment (CI/CD to Azure)

> How an already configured AI4IA environment promotes a commit from `main` to
> Azure, proves the running digests, and rolls back failed application revisions.
>
> **New subscription or tenant?** Start with
> [`greenfield-standup.md`](./greenfield-standup.md). This runbook assumes its
> identity, repository variables, Entra registrations, first provision, and
> optional custom-domain cutover are complete.
>
> Source of truth: `.github/workflows/deploy.yml`, `azure.yaml`,
> `infra/main.bicep`, and `infra/main.parameters.json`.

## TL;DR - does merging to `main` redeploy?

Yes, when deployment is not explicitly disabled. A push to `main` that touches
application, infrastructure, proxy, Foundry, deployment workflow, or directly
executed release-script paths runs `.github/workflows/deploy.yml`.
Documentation-only changes do not deploy; use **Actions → deploy → Run
workflow** when an explicit redeploy is needed.

The workflow validates `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID`, `AZURE_ENV_NAME`, and `AZURE_LOCATION` before the
deployment job can start. Missing values fail the workflow with their names;
they never produce a successful skipped job. A repository with no deployment
target must declare that posture with `AI4IA_DEPLOYMENT_ENABLED=false`.

## Moved setup sections

The former one-time setup and tenant-migration sections now live in the
[greenfield standup guide](./greenfield-standup.md). These compatibility anchors
preserve old inbound links while keeping setup instructions in one place:

<a id="2-one-time-setup-make-merges-deploy"></a>
<a id="25-custom-domains-vanity-hostnames--required-if-you-use-them"></a>
<a id="26-web-iq-api-key-secret"></a>
<a id="27-application-entra-app-registrations-required-when-ai4ia_auth_providerentra"></a>
<a id="3-moving-to-a-new-subscription-or-tenant-11-standup"></a>
<a id="3a-custom-domains-bind-them-after-the-first-provision-not-during-it"></a>

- [Subscription, provider, quota, and model preflight](./greenfield-standup.md#1-preflight-the-target)
- [Deployment identity, both GitHub OIDC subjects, and RBAC](./greenfield-standup.md#2-create-the-deployment-identity-and-oidc-trust)
- [Repository variables and secrets](./greenfield-standup.md#3-configure-github)
- [Application Entra registrations and consent](./greenfield-standup.md#4-create-the-application-entra-registrations)
- [First provision and two-phase custom domains](./greenfield-standup.md#6-provision-in-two-phases)
- [Tenant migration and first validation](./greenfield-standup.md#8-migrate-an-existing-deployment)

## 1. Exact-digest release flow

```text
push to main (app/infra/proxy/azure.yaml)       manual workflow_dispatch
                    |                                   |
                    +----------------+------------------+
                                     v
                         deploy.yml (production)
                                     |
                                     v
                    OIDC login and repository checks
                                     |
                                     v
              provider + custom-domain safety preflights
                                     |
                                     v
               capture pre-provision Container App revisions
                                     |
                                     v
                       azd provision --no-prompt
                                     |
                                     v
             build/push web + api + proxy once at commit SHA
                                     |
                                     v
                  resolve and record registry digests
                                     |
                                     v
       azd deploy <service> --from-package <registry>@sha256:<digest>
                                     |
                                     v
          verify exact images, rollout, health, domains, and model canary
                                     |
                          failure ---+---> restore captured revisions
```

The capture happens **before `azd provision`**, not after the build. This ordering
is load-bearing: each Container App Bicep module must submit an image, and a
greenfield template can use a quickstart placeholder. Provisioning can therefore
create a new placeholder revision before release images exist. Capturing after
provision would record that placeholder as the rollback target.

The workflow then builds each service once, tags it with the commit SHA, pushes
it to the azd-managed ACR, reads the digest assigned by the registry, and deploys
that fully qualified digest. `azd deploy --from-package` receives a registry
reference and therefore does not rebuild or repush it. The job summary records
all three immutable references.

Other operational properties:

- Deploys are serialized in the `deploy-production` concurrency group and are
  not cancelled mid-flight.
- Push deployments always reconcile infrastructure. A manual run may uncheck
  **provision**.
- A local `azd deploy` still builds from the workstation and does not execute the
  workflow's verification or rollback gate.
- Rollback restores Container App revisions, not infrastructure. APIM policies,
  fragments, named values, model deployments, and RBAC changed by provision must
  be fixed forward or explicitly reverted and reprovisioned.

That last limit is measured, not hypothetical. On 2026-08-05 a duplicate backend
label in the generated APIM catalog made model requests return 500. Seven
consecutive deployments restored the containers but could not restore the APIM
policy; chat recovered only after the policy was corrected and provisioned.

## 2. Before a routine deployment

### 2.1 Confirm configuration and generated artifacts

The deploy workflow runs the catalog, gateway-policy, and prerequisite checks
before provisioning. Run the same checks before merging changes to those inputs:

```powershell
python scripts/gen-model-catalog.py --check
python scripts/gen-mcp-catalog.py --check
python scripts/gen-voice-provider-catalog.py --check
python scripts/gen-gateway-policy.py --check
python scripts/validate-feature-prereqs.py
```

Feature flags and their fail-closed prerequisites are maintained in
[`feature-enablement.md`](./feature-enablement.md). Repository variables only
reach Bicep when `.github/workflows/deploy.yml` exports them and
`infra/main.parameters.json` consumes the matching token; the configuration
reachability test guards that contract.

#### Claude Marketplace attestation

Claude is a default-off entitlement. Set `AI4IA_CLAUDE_ENABLED=true` only after
the subscription can complete Anthropic Marketplace fulfillment. Enabling it
makes all three attestation variables mandatory:

| Variable | Meaning |
|---|---|
| `AI4IA_CLAUDE_ENABLED` | Deploy and advertise Claude (`false` or unset omits it) |
| `AI4IA_CLAUDE_ORGANIZATION_NAME` | Legal entity accepting Anthropic's Marketplace terms |
| `AI4IA_CLAUDE_COUNTRY_CODE` | Uppercase ISO-2 country code |
| `AI4IA_CLAUDE_INDUSTRY` | Lowercase Foundry Marketplace industry value |

`modelProviderData` sends those values with each Claude deployment and
auto-accepts the offer. The workflow and `validate-feature-prereqs.py` fail
before `azd provision` when Claude is enabled and a value is missing, malformed,
or placeholder-shaped. With the gate off, the preflight, Bicep deployment list,
postprovision expected-deployment list, and runtime model catalog all omit
Anthropic entries.

Claude Opus 4.8 is pinned to Azure-hosted version 2 in East US 2. The catalog
allocates the production subscription's measured quota: Global Standard 40K TPM
and US Data Zone Standard 13K TPM. Re-run
`python scripts/check-model-availability.py --region eastus2` immediately before
provision because the counters are subscription/data-zone shared and can change.

Microsoft internal/sandbox subscriptions can still reject the paid Marketplace
fulfillment after model access/quota approval. Treat
`This subscription is internal or sandbox` / `Marketplace purchases are
disabled` as a Marketplace-fulfillment issue, not a quota increase. Keep
`AI4IA_CLAUDE_ENABLED=false`, reopen the approved Anthropic Model Access case,
and enable only after a direct deployment attempt succeeds.

### 2.2 Protect existing custom domains

For an environment with vanity hostnames, every provision must receive all four
values:

| Variable | Purpose |
|---|---|
| `AI4IA_WEB_CUSTOM_DOMAIN` | Web hostname |
| `AI4IA_WEB_MANAGED_CERT_NAME` | Existing web managed certificate |
| `AI4IA_PROXY_CUSTOM_DOMAIN` | Proxy hostname |
| `AI4IA_PROXY_MANAGED_CERT_NAME` | Existing proxy managed certificate |

An empty custom-domain value tells Bicep to set `customDomains: []`; it does not
mean "leave the current binding unchanged." The workflow's custom-domain
preflight fails before capture/provision when a live binding exists but its
variable is missing. DNS remains external to Azure.

Current production evidence:

| Setting | Value |
|---|---|
| Tenant | Planet Express `6907d2a4-685a-4aea-92ab-d930217467f1` (Entra can still display "Contoso") |
| Subscription | `sub-planetexpress-slurmfactory` / `e852113b-6cb5-441c-ac68-26cff884e479` |
| Resource group | `rg-ai4ia-slurmfactory` |
| Web domain / certificate | `ai4ia.nomad-analytics.com` / `mc-cae-ai4ia-slur-ai4ia-nomad-anal-2891` |
| Proxy domain / certificate | `genaiproxy.nomad-analytics.com` / `mc-cae-ai4ia-slur-genaiproxy-nomad-6552` |

First-time bindings belong in the
[greenfield two-phase sequence](./greenfield-standup.md#62-bind-custom-domains-optional).
The workflow registers a new hostname before provision; a local provision needs
the documented `az containerapp hostname add` step.

### 2.3 Validate gateway direction

After gateway changes and before application smoke tests, confirm:

1. `AZURE_MODEL_GATEWAY_URL` is the proxy `/openai` URL.
2. `AZURE_APIM_GATEWAY_URL` is different and is not an application model URL.
3. APIM's `openai` API terminates at Foundry, never at `ca-proxy`.
4. The proxy `Host1` terminates at APIM and holds its subscription key as an ACA
   secret.
5. Voice Live uses the FastAPI relay to its provider-specific APIM WebSocket
   route, never SimpleL7Proxy.
6. `AZURE_PROXY_APP_NAME` names the Container App used for revision inspection
   and rollback.

Do not add a second model-write retry loop. APIM owns bounded same-dispatch
regional failover; SimpleL7Proxy owns delayed requeue through `S7PREQUEUE`.

## 3. Trigger or perform a deployment

Normal releases are merges to `main`. To rerun the workflow:

```powershell
gh workflow run deploy.yml -f provision=true --ref main
```

For a manual break-glass deployment from a workstation:

```powershell
az login
azd env select <environment>
azd provision
azd deploy
```

The App Configuration sentinel is owned by the OIDC deployment identity. A local
`azd provision` therefore leaves an existing sentinel unchanged when
`AZURE_PRINCIPAL_ID` is absent; it does not wait on or fabricate a data-plane role
for the signed-in human. Use the workflow for greenfield setup or any repair that
must reconcile the sentinel.

This local path rebuilds and has no automatic post-deploy rollback. Prefer the
workflow when the goal is a production release with recorded digest evidence.
Validate potentially destructive infrastructure changes in an isolated
environment first. Full-catalog validation requires a separate subscription when
subscription-wide model quota is consumed; otherwise use the explicitly reduced
profile and claims in the [teardown runbook](./teardown.md#1-validate-iac-without-pretending-subscription-wide-quota-is-duplicable).

## 4. APIM policy changes and gateway canaries

ARM what-if validates resources, not APIM's policy-expression compiler. A policy
fragment `PUT` can also return `201 InProgress` before its async operation fails.
Before merging a policy-fragment change, run the disposable compiler harness
against the target APIM under separate explicit approval:

```powershell
.\scripts\test-apim-policy-compiler.ps1 `
  -SubscriptionId <subscription-id> `
  -ResourceGroup <resource-group> `
  -ServiceName <apim-service-name>
```

The harness creates uniquely named temporary fragments and an API, compiles the
full production include chain, deletes only those temporary names in `finally`,
and verifies cleanup. It never changes a production API, policy, or fragment.
This runbook does not claim the live harness has been run.

`ca-proxy` uses single-revision mode, so validate gateway changes in an isolated
azd environment rather than attempting weighted traffic inside the production
app. Follow the same separate-subscription/reduced-profile quota boundary above:

1. Deploy the branch with a separate proxy hostname.
2. Verify proxy `/startup`, `/liveness`, and `/readiness`.
3. Send one non-streaming and one streaming model request through the proxy.
4. Run the authenticated Voice Live canary for each enabled provider/model, then
   perform a signed-in browser microphone retest.
5. Confirm APIM diagnostics show the proxy subscription for HTTP/SSE and the
   correct distinct subscription for each realtime route.
6. Move production DNS/custom-domain bindings only after those checks pass.

Use an operator-obtained Entra API token in an environment variable, never a CLI
argument. Derive the socket from `AZURE_API_URL`, which is the direct FastAPI
Container App origin:

```powershell
$apiUrl = azd env get-value AZURE_API_URL
$voiceUrl = ($apiUrl -replace '^https://', 'wss://').TrimEnd('/') + '/api/voice/live'

python scripts/voice-live-canary.py `
  --url $voiceUrl `
  --origin https://<web-origin> `
  --provider azure_openai `
  --model gpt-realtime `
  --region eastus2 `
  --token-env AI4IA_VOICE_CANARY_TOKEN
```

Repeat without `--region` for each enabled Speech managed model. A successful
protocol canary receives `session.created` and `session.updated`; it sends no
audio or conversation content. Direct APIM handshakes are infrastructure
diagnostics, not proof of the authenticated app path. Never use the web/Next.js
hostname for `--url`: it cannot proxy WebSockets. The HTTPS web origin remains
the correct `--origin` value.

For a policy regression, restore the known-good source and run `azd provision`
as well as `azd deploy`. Restoring a Container App revision does not change APIM.
The preserved emergency policy
`infra/policies/simplel7proxy-rollback-policy.xml` requires a separately
authorized operator change and is never deployed automatically by Bicep.

Generated policy fragments use content-addressed names. Incremental deployment
retains older generations intentionally. Keep the active generation and one
known-good rollback generation; deleting older fragments is a separate
destructive operation requiring approval.

## 5. Data recovery posture

Application rollback does not restore data.

| Property | Current posture |
|---|---|
| Cosmos backup mode | `Continuous7Days`, declared in `infra/modules/data.bicep` |
| Recovery point | Any second within the last seven days |
| Restore mechanism | Self-service `az cosmosdb restore` |
| Restore target | A new serverless account; the source is untouched |
| Backup storage charge | No charge for the 7-day tier; restore operations are billed |

This was verified with a throwaway serverless account: enabling continuous
backup and restoring it produced a new serverless account with the container and
partition key intact. The contrary historical claim that serverless required a
support-ticket restore confused continuous backup with Azure Backup vaulted
backup, which is a different feature.

```bash
az cosmosdb restore \
  --resource-group <resource-group> \
  --account-name <source-account> \
  --target-database-account-name <new-account-name> \
  --restore-timestamp <UTC-timestamp> \
  --location <region>
```

The source must contain data at the chosen time. A failed restore reserves the
target name in a failed state until that target is deleted. Repoint
`AI4IA_COSMOS_ENDPOINT` or copy only the affected documents after validating the
restored account.

Changing backup mode must be a standalone operation; Azure rejects a backup-mode
change bundled with other account property updates. The live account was moved
with `az cosmosdb update`, and Bicep now restates the resulting mode.

Key Vault has seven-day soft delete. Purge protection defaults off so a teardown
can recreate the name; setting `AI4IA_KEYVAULT_PURGE_PROTECTION=true` is
irreversible and prevents same-name recreation during the retention period.
Blob source files and generated media have no equivalent restore path here.
Document chunks, search indexes, and parsed artifacts are derived and rebuildable.

## 6. Post-deploy verification and rollback

Every workflow deployment is gated. The implementation is
[`scripts/post-deploy-verify.py`](../../scripts/post-deploy-verify.py), with
contract tests in `scripts/tests/test_post_deploy_verify.py`.

### Why the gate exists

`azd deploy` returning zero proves ARM accepted a Container App template, not
that the application runs. A crash-looping image, missing secret, import error,
stale APIM route, or catalog/policy mismatch can all produce a green deployment.
The `azure.yaml` postprovision hook runs before application deployment and cannot
close this gap.

The workflow captures active revisions before provision. If provision starts,
later provision, build, preflight, deploy, or verification failures restore the
pre-provision revisions. A manual workflow run that skips provision does not
roll back for a build/preflight failure that touched no app. Deploy and
verification failures still roll back.

A failure to reacquire the **post-deploy** canary token is the deliberate
exception: the exact-digest release remains live but is reported unverified.
The grant is preflighted before deploy, and verification acquires a fresh token
after deploy so an expired token cannot roll back a healthy release.

### What verification asserts

| Assertion | Failure it detects |
|---|---|
| Active revision moved when expected | Deployment never promoted a new template |
| Running image equals this run's `--expect-image` digest | A stale or unrelated revision is serving |
| Revision is active, healthy, and running | ARM accepted a revision that never became ready |
| Running replicas are positive where minimum replicas require them | Crash loop or failed replica startup |
| API `/health/live` and `/health/ready` return 200 | Process failure or inability to reach the canonical session store (cached, bounded check) |
| Web `/` returns 2xx/3xx without following redirects | Next.js failed to render |
| Proxy ingress returns below 500 | No proxy replica is serving |
| Configured domains remain bound and `SniEnabled` | Provision removed a binding or certificate failed |
| Authenticated models/session/chat/cleanup turn succeeds | Entra, catalog, proxy, APIM, and Foundry path failure |

The assertions share a 20-minute wall-clock budget inside a 30-minute step
timeout. The canary intersects `infra/models.json` with the models the live API
advertises, never hardcodes a deployment, never prints its bearer token or model
reply, and logs successful replies only as a character count.

The same proof can run independently of a deployment or rollback state file:

```powershell
$env:AI4IA_DEPLOY_VERIFY_TOKEN = '<short-lived Entra bearer token>'
python scripts/post-deploy-verify.py canary --api-url https://<api-fqdn>
```

This canary-only mode authenticates, lists models, creates a Cosmos-backed
session, completes one governed FastAPI -> proxy -> APIM -> Foundry turn, and
deletes the session. It emits only model id, reply length, elapsed time, and
bounded/redacted failure detail; cleanup failure fails the command.

It does not assess response quality, streaming, tools, MCP, documents, memory,
media, or realtime. It is one identity and cannot detect per-user entitlement or
ownership defects. Rollback does not run after manual cancellation or the
180-minute job timeout because the runner is terminated.

### Automatic and manual rollback

For each app whose active revision moved, rollback restores the captured revision
and confirms the captured image is serving again. A failed confirmation remains
a failed job.

| Revision mode | Restore primitive |
|---|---|
| `Single` (this stack) | `az containerapp revision copy --from-revision <captured>` |
| `Multiple` | `az containerapp ingress traffic set --revision-weight <captured>=100` |

A greenfield app has no prior revision and cannot be rolled back on its first
deployment. An app that did not move is left unchanged.

To disable only the end-to-end model turn, set
`AI4IA_DEPLOY_VERIFY_CANARY=false`. Rollout, health, web, proxy, and domain checks
still run. An empty audience never silently disables the canary; without the
explicit variable, preflight fails.

Manual recovery:

```powershell
$rg = '<resource-group>'
az containerapp revision list -g $rg -n <container-app> `
  --query "[].{name:name,created:properties.createdTime,active:properties.active}" -o table
az containerapp revision copy -g $rg -n <container-app> --from-revision <previous>
```

Redeploying a known-good commit through the workflow is preferable because it
also re-verifies the release. If an infrastructure regression persists after
containers roll back, correct or revert the infrastructure source and provision
again.

## 7. Troubleshooting


> Entry numbers are stable identifiers, not positions — they are referenced from
> other docs, from commit messages, and from CI failure output. A retired entry
> leaves its number vacant rather than renumbering everything below it.
> §7.1 (Postgres `LocationIsOfferRestricted`) was retired with the PostgreSQL server on 2026-08-06.
> §7.7 (Postgres `ServerIsBusy`) was retired at the same time.

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

Speech Voice Live never falls back to Azure OpenAI, the proxy, or another host on
failure — a failed Speech connection surfaces a bounded error to the browser rather
than silently degrading to a different provider or deployment.

The safe protocol-error/close capture, correlation/outcome/frame-count completion
record, deterministic cleanup, retry messaging, and isolated inline selectors are
confirmed code-level diagnostics/UX fixes. Authenticated protocol canaries for
both `azure_openai/gpt-realtime` and `speech_voice_live/gpt-realtime` succeeded on
2026-08-08. For a new failure, correlate the canary output with API and APIM
diagnostics before assigning an upstream service or model root cause; the
signed-in microphone retest remains the end-to-end audio/UX check.

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
idempotent, exactly like §7.2):

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

Prevention — `scripts/check-model-availability.py` checks lifecycle as a third axis
alongside availability and quota. `azure.yaml` runs it automatically in `preprovision`,
before ARM creates shared or paid resources. The check is existing-state-aware: it
lists only the target environment's Foundry accounts/deployments and permits an exact
`Succeeded` deployment (same name, model, version, SKU, capacity, and version-upgrade
posture) with a loud migration warning. A greenfield/addition, missing deployment, or
any drift still blocks because provision would create or change the deprecated model.
The same rule applies when Azure no longer lists the model/SKU or quota has since
fallen below its existing capacity: an all-exact group warns and reconciles without
adding capacity, while a mixed group with any new/drifted deployment evaluates the
full desired capacity and remains blocking.
If target identity is ambiguous or inventory fails, the check fails rather than guessing.
Run it directly for diagnosis ([greenfield preflight](./greenfield-standup.md#1-preflight-the-target)).
It reports whether a deployable version exists to repin to, or that the model must be removed.

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
for non-OpenAI models and a **warning** for OpenAI ones. The azd `preprovision` hook runs
the full check without `--skip-quota`; missing Azure CLI credentials or a CLI subscription
that differs from `AZURE_SUBSCRIPTION_ID` fails before resource creation.

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

The full cutover sequence is in
[greenfield standup §6.2](./greenfield-standup.md#62-bind-custom-domains-optional).

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

### 7.16 Responses-API turns leave a 30-day copy of user content at the provider

Applies to the models the catalog marks `"api": "responses"` (the flagship
reasoning models that `400` on chat completions — see §7.11). Nothing fails; this
is a silent governance gap, so it will not surface as an error.

**Cause.** The Responses API has a `store` parameter that chat completions has no
equivalent of, and it **defaults to `true`**. Every turn therefore leaves the
prompt and the model's output retrievable from `GET /responses/{id}` for 30 days,
outside the app's own store. AI4IA keeps conversation state in Cosmos scoped per
user (`AGENTS.md` rule 4) and this client re-sends full history each turn rather
than chaining with `previous_response_id`, so the retained copy is never read back
— it is purely a second, ungoverned store of user content.

**Fix (already in the client).** `build_responses_request` sends `"store": false`
on every Responses turn, streaming and non-streaming alike. Pinned by
`test_responses_requests_opt_out_of_provider_side_storage`.

**Confirm** against a Foundry account directly (this is a data-plane property, so
it is easiest to see without the gateway in the way):

```powershell
$key  = az cognitiveservices account keys list -n <account> -g <rg> --query key1 -o tsv
$base = "https://<account>.cognitiveservices.azure.com/openai"
$body = '{"model":"<deployment>","input":[{"role":"user","content":"say pong"}],"max_output_tokens":16384,"store":false}'
$r = curl.exe -s -X POST "$base/responses?api-version=2025-04-01-preview" `
  -H "api-key: $key" -H "Content-Type: application/json" --data $body | ConvertFrom-Json
$r.store                                    # expect False
curl.exe -s -o NUL -w "%{http_code}" -X GET "$base/responses/$($r.id)?api-version=2025-04-01-preview" -H "api-key: $key"
```

Expect `store: False` and **404** on the retrieval. A `200` means the turn was
stored: `store` has been dropped from the request body.

**If turn chaining is ever added**, do not flip `store` back to get reasoning
continuity. Request `include: ["reasoning.encrypted_content"]` and pass the
encrypted reasoning items forward — that is the stateless-mode equivalent and
keeps the content in the app's control.

## Switching the search index tenancy model

`AI4IA_SEARCH_INDEX_PER_USER` (default `true`) chooses between an index per user
(`<AI4IA_SEARCH_INDEX_NAME>-u<sha256(user_id)[:32]>`) and one shared index
filtered by `user_id`. The `user_id` filter is applied on every query in **both**
modes, so isolation never depends on index routing alone.

**Flipping this value strands existing chunks.** Nothing is deleted, but reads go
to the other index, so every already-ingested document silently stops being
retrievable. Chat still answers — it just answers without the library. Plan the
switch as a migration, not a config change.

The chunk index is derived state: Cosmos holds the manifests and Blob holds the
raw bytes plus `parsed.md`, so it is always rebuildable. Re-running enrichment on
a document rebuilds its chunks into whichever index the store now resolves,
because `_persist_enrichment` deletes then re-adds.

Sequence:

1. Set the value and deploy.
2. Rebuild each existing document so its chunks land in the new index:

   ```bash
   # Per caller, against their own library. Rebuilds every ready document.
   curl -X POST https://<api-host>/api/library/documents/reindex \
     -H "Authorization: Bearer <token>"
   ```

   This re-embeds from the stored `chunks.jsonl`/`parsed.md`; it does **not**
   re-run Content Understanding or Mistral, so the migration costs embeddings
   rather than a second analysis pass. The response reports per-document
   outcomes, so one unrebuildable document does not strand the sweep. Newly
   uploaded documents need nothing.
3. Optionally delete the now-orphaned index. The old one keeps costing storage
   and counts against the tier's index limit until it is removed:

   ```bash
   az rest --method delete --url \
     "https://<service>.search.windows.net/indexes/<old-index>?api-version=2024-07-01" \
     --resource https://search.azure.com
   ```

### The index limit is a ceiling on users

With per-user indexes the tier's **index** limit becomes a limit on **users**:

| Tier | Max indexes | Max users in this mode |
|---|---|---|
| basic | 15 | 15 |
| standard (S1) | 50 | 50 |
| S2 / S3 | 200 | 200 |
| S3 HD | 1000 per partition | designed for this pattern |

The user who would be number 51 on S1 cannot upload at all — index creation is on
the ingest path. Azure reports it as a generic 400 that never mentions tenancy,
so the store raises `SearchIndexQuotaError` naming both remedies: raise the tier,
or set `AI4IA_SEARCH_INDEX_PER_USER=false`.

Microsoft's own guidance is that a shared index with a tenant filter is the
cheaper pattern at small and medium scale, and that dedicated indexes suit
enterprise tiers or tenants past a size threshold. This deployment chose
per-user deliberately, with the ceiling accepted.

## Changing the Azure AI Search tier

`AI4IA_SEARCH_SKU` (default `standard`, i.e. S1) and `AI4IA_SEARCH_SEMANTIC_PLAN`
(default `standard`) are deployment parameters, but they behave **differently** on
an existing service and the difference matters.

| Change | On a new service | On an existing service |
|---|---|---|
| `searchSemanticPlan` (`free` -> `standard`) | Applied at create | Applied in place — it is a mutable property |
| `searchSku` (`basic` -> `standard`) | Applied at create | Applied in place, but **only through the management API/CLI/portal**, not by `azd provision` |

Azure supports moving an existing service between Basic and Standard (S1/S2/S3),
upgrades and downgrades, since July 2025. The capability landed in the
`2025-02-01-preview` Update Service API; the Bicep resource here is pinned to
`2023-11-01`, so do not assume a provision run performs the tier change. Drive it
explicitly and then align the parameter.

Verified on 2026-08-14 against the live `slurmfactory` service, moving
`basic` -> `standard` (S1):

```bash
# Semantic plan: fast, no downtime.
az search service update -n <service> -g <resource-group> --semantic-search standard

# Tier: a long-running operation. Took ~10 minutes end to end.
az search service update -n <service> -g <resource-group> --sku standard
```

During the tier change `provisioningState` reports `Provisioning` and `status`
reports `provisioning`; both return to `Succeeded`/`running` when it completes.
**The index survived the upgrade** — `ai4ia-doc-chunks` was present and intact
afterwards, so no reindex was required. Confirm with:

```bash
az search service show -n <service> -g <resource-group> \
  --query "{sku:sku.name,semantic:semanticSearch,state:provisioningState}"
```

Then set `AI4IA_SEARCH_SKU` / `AI4IA_SEARCH_SEMANTIC_PLAN` to match so IaC and
reality agree; leaving them behind means the next greenfield standup silently
provisions a different tier than production runs.

Do **not** delete and recreate the service to force a tier change. The service
name is derived from `uniqueSuffix`, so a recreate reuses the name but starts
empty. The chunk index is derived state and is rebuildable from the Cosmos
manifests plus the blob artifacts, so it is recoverable — but retrieval returns
nothing until the rebuild completes, which is a user-visible outage that the
in-place upgrade above avoids entirely.

Cost, from the Azure retail price API (East US, checked 2026-08-14):

| | Basic | Standard S1 |
|---|---|---|
| Service | ~$0.10/hour (~$73/month) | ~$0.34/hour (~$248/month) |

The `standard` semantic plan is billed per query (~$1 per 1,000 semantic queries)
rather than as a monthly unit, and replaces the `free` plan's hard cap of 1,000
semantic queries/month. The retrieval breaker in `ai_search_chunks.py` is retained
on the paid plan because throttling and service errors present the same way as an
exhausted quota.

## APIM plane (single Basic v2 service)

The model/realtime/MCP plane is the `apim-mcp-<workload>-<environmentName>-<uniqueSuffix>` Basic v2 service (capacity 1), and it is the **only** APIM service in the environment. `apimcore.bicep` owns its identity and single diagnostic setting; `mcpgateway.bicep` owns the MCP children and `gateway.bicep` adds model/realtime children through the shared contract. When `speech_voice_live` is enabled, that same service also carries its WebSocket API (`/speech/voice-live/realtime`) and distinct subscription (`ai4ia-api-speech-voice-live`) alongside the Azure OpenAI `/openai/realtime` API and subscription. MCP uses an MCP-only product/subscription, so its key cannot call model/realtime APIs; each enabled voice provider also has a distinct key. Configure APIs, policies, keys, and Foundry RBAC before caller revisions update.

MCP, HTTP/SSE, Azure OpenAI Realtime, and any enabled Speech Voice Live objects
share the service's blast radius and resilience posture. Any change that touches
APIM still gets a **zero-delete what-if review** before it is applied.

The superseded PR-only `apim-v2-*` design was never deployed. A what-if must contain
no `apim-v2-*` creation. If resource inventory unexpectedly finds such a service, stop
and handle it through a separate explicitly approved cleanup.

<a id="speech-voice-live-posture-off"></a>
<a id="speech-voice-live-posture"></a>

### Speech Voice Live posture

The template remains default-off (`speechVoiceLiveEnabled=false`). The observed
environment sets `AI4IA_SPEECH_VOICE_LIVE_ENABLED=true` and
`AI4IA_VOICE_PROVIDER_ALLOWLIST=azure_openai,speech_voice_live`; authenticated
`speech_voice_live/gpt-realtime` and `azure_openai/gpt-realtime` canaries returned
`outcome=success` on 2026-08-08. Current enablement details live in
[`feature-enablement.md`](./feature-enablement.md#speech-voice-live-second-voice-provider);
do not infer them from retained APIM objects.

The canary target must be the `wss://` form of `AZURE_API_URL` with
`/api/voice/live`, never the web/Next.js hostname. Follow
[`feature-enablement.md`](./feature-enablement.md#speech-voice-live-second-voice-provider)
for the standing APIM review rules and the signed-in microphone regression check.
If the provider is disabled later, ARM incremental mode can retain its Speech
objects; inventory alone is not evidence of enablement.

### Deleted APIM name reuse

The retired Consumption APIM is gone from Azure and IaC; the shared Basic v2
`apim-mcp-*` service is the only APIM plane. Migration history is in
[`CHANGELOG.md`](../../CHANGELOG.md).

**If the name must be reused,** an APIM delete leaves the service soft-deleted and
holding its globally-unique name for 48 hours. Purge it first:

```bash
az apim deletedservice list -o table
az apim deletedservice purge --location <region> --service-name <name>
```

This does not apply to a normal redeploy of the surviving `apim-mcp-*` service.
