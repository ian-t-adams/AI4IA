# Runbook: Feature Enablement

Most advanced AI4IA surfaces are implemented but gated. Defaults in code/Bicep
are safe; the checked-in showcase profile in `infra/main.parameters.json`
enables many of them through overridable `${AI4IA_*=true}` bindings. The live
posture is controlled by azd/repository values and Container App env. Startup validation in
`app/api/src/ai4ia_api/config.py` fails closed for half-wired deployed features.
Use the consolidated parameter/env map in
[`../configuration-reference.md`](../configuration-reference.md) before changing
feature posture.

> **Policy approval and implementation evidence are different.** The
> [Responsible AI decision record](../rai-decision-record.md) records the
> 2026-09-03 owner direction for the named providers/modalities: show assessments
> without adding application-level blocking. Assessment coverage, disclosure,
> aggregate monitoring, and escalation remain incomplete. A new provider or
> modality outside that decision still requires review; an enabled gate is not
> evidence that all safety signals are captured.

## Flag inventory


| Feature | API flag / setting | Web flag | IaC parameter | Deployed prerequisites |
|---|---|---|---|---|
| Cross-tenant Claude | `AI4IA_CLAUDE_ENABLED` + `AI4IA_CLAUDE_EXTERNAL_ENABLED` | safe server catalog only | `claudeEnabled`, `claudeExternalEnabled`, `claudeBindingJson` | Defaults off/unconfigured. Separate target account/models, explicit legal/network decision, source UAMI + multitenant app/FIC, target SP + exact-account AIServices inference role, distinct target reader and fresh both-tenant readbacks. Not Private Link or live approval. |
| Atomic request-count admission | `AI4IA_HARD_QUOTA_ENABLED` | none | `hardQuotaEnabled`, `hardQuotaRolloutId` (`AI4IA_HARD_QUOTA_ROLLOUT_ID`) | Default `false`; outside the local test fake needs Entra, Cosmos and the approved rollout record selected by `AI4IA_HARD_QUOTA_ROLLOUT_ID` (startup validates evidence shape and layout). Request-count only; owners need an operator bootstrap; drain before activation. See the note below |
| Versioned one-attempt gateway staging | `AI4IA_GATEWAY_ATTEMPTS_V1_STAGED` | none | `gatewayAttemptsV1Staged` | Default `false`; stages only the isolated API/operations/policy/scoped proxy key on the existing APIM. Governed HTTPS native proxy ingress and S7P-KEY auth required; no shipping runtime verifier or cap activation. See [construction prerequisites](../hard-quota-admission.md#versioned-route-staging-and-construction-contract) |
| Voice Live | `AI4IA_REALTIME_ENABLED` | `VOICE_LIVE_ENABLED` + `API_PUBLIC_URL` | `voiceLiveEnabled` | Browser Origin allowlist outside local |
| Voice Live tools | `AI4IA_REALTIME_TOOLS_ENABLED` | advertised by web env | `voiceLiveToolsEnabled` | Voice Live enabled |
| Staged GA Realtime | `AI4IA_REALTIME_GA_ENABLED` + `AI4IA_REALTIME_PROTOCOL` | read-only `openaiRealtimeProtocol` from API config | `realtimeGaEnabled` + `realtimeProtocol` | Defaults `false` + `preview`; Voice Live, distinct GA APIM URL/key; approved canary before selection/cutover |
| Speech Voice Live (2nd voice provider) | `AI4IA_SPEECH_VOICE_LIVE_ENABLED` | advertised by web env | `speechVoiceLiveEnabled` | Voice Live enabled; `speech_voice_live` in `AI4IA_VOICE_PROVIDER_ALLOWLIST`; distinct `AI4IA_SPEECH_VOICE_LIVE_BASE_URL` + `AI4IA_SPEECH_VOICE_LIVE_GATEWAY_API_KEY`; repeat the standing APIM and authenticated-canary checks after changes |
| Document library + multimodal understanding | `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED` | `DOCUMENT_LIBRARY_ENABLED` | `documentUnderstandingEnabled` | Cosmos session store, Blob, CU, Search endpoint and catalog-resolved embedding deployment outside local; preprovision requires `searchEnabled=true` |
| CU synchronous/preview analyzers | `AI4IA_CU_PREVIEW_ENABLED` | analyzer selector | `cuPreviewEnabled` | Document understanding plus successful postprovision GETs for Read, Layout, and the five tax analyzers on `2026-06-01-preview`. Automatic stays GA. |
| CU Agentic document reasoning | `AI4IA_CU_AGENTIC_ANALYZER_ID` | analyzer selector only when valid | `cuAgenticAnalyzerId` | Preview enabled; existing analyzer resolves to `agentic.*`; effective primary GPT-5.2 deployment capacity ≥400K TPM. The 50K baseline is insufficient; capacity alone does not configure an analyzer. |
| Library compute / export | `AI4IA_DOCUMENT_COMPUTE_ENABLED` | none | `documentComputeEnabled` | Document understanding, dedicated Code Interpreter APIM URL/key + model outside local |
| Inline attachment Code Interpreter | `AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED` | none | `inlineDocumentComputeEnabled` | Dedicated Code Interpreter APIM URL/key + model outside local |
| Azure AI Search chunk store | `AI4IA_SEARCH_ENDPOINT` set | none | `searchEnabled` + `searchLocation` | Search service + API identity RBAC |
| Memory / semantic recall | `AI4IA_MEMORY_STORE=cosmos` | inspector create/edit/delete controls | `memoryStore` | Cosmos endpoint/database, vector capability/container, and catalog-resolved embedding/extraction models |
| Rolling conversation summarization | `AI4IA_AUTO_SUMMARIZATION_ENABLED` | none | `autoSummarizationEnabled` | None beyond the active chat model — once the transcript exceeds the model-derived threshold, older turns fold into a running summary while the full transcript stays in storage/scrollback. Off leaves the manual `/summarize` command working but never auto-injects a summary |
| Image generation | `AI4IA_IMAGE_GENERATION_ENABLED` | server-advertised imagery controls | `imageGenerationEnabled` | Image-capable deployment and durable media Blob storage outside local; storage presence alone does not enable generation |
| Video generation | `AI4IA_VIDEO_GENERATION_ENABLED` | server-advertised tools and inline artifacts | `videoGenerationEnabled` | A runtime-enabled video deployment and durable media Blob storage outside local. Advertisement and execution check the gate, the store and model availability together. Sora 2 is runtime-disabled ahead of its 2026-10-15 retirement, so the tool stays hidden. Keep the flag on: it also delivers the Blob settings that serve existing clips (see [Sora 2 runtime retirement](deployment.md#sora-2-runtime-retirement)) |
| Custom photo avatars | `AI4IA_PHOTO_AVATARS_ENABLED` (+ per-user and live-session limits) | availability from `GET /api/photo-avatars/config` | `photoAvatarsEnabled`, `photoAvatarMaxPerUser`, `photoAvatarMaxCreationsPerDay`, `photoAvatarLiveMaxMinutesPerSession`, `photoAvatarLiveIdleTimeoutSeconds` | Default `false`. Entra, Cosmos, durable Blob and metering outside local. Creation also needs the home account to report the Limited Access capability at runtime. Live avatar sessions also need Speech Voice Live. The approval, the RAI re-approval and the live checks come first: see [below](#custom-photo-avatars) |
| Custom MCP tools | `AI4IA_CUSTOM_TOOLS_ENABLED` | `CUSTOM_TOOLS_ENABLED` | `customToolsEnabled` | Cosmos, Key Vault URI, Entra auth outside local |
| Official MCP plane | `AI4IA_OFFICIAL_MCP_ENABLED` | none | `enableOfficialMcp` | MCP-only product/subscription on the shared active Basic v2 APIM + ≥1 server in `infra/mcp-servers.json`; gateway URL + key auto-wired |
| Foundry toolbox (bridge) | consumed via the official MCP plane (no dedicated flag) | none | `enableFoundryToolbox` (+ `enableOfficialMcp`) | Provisioned toolbox in the default Foundry project + a `foundry-toolbox` entry in `infra/mcp-servers.json`; grants APIM MI the project "Foundry User" role. See [`../foundry-toolbox.md`](../foundry-toolbox.md) |
| Private tool catalog (API Center) | admin/IaC only (no app-runtime env) | none | `enablePrivateToolCatalog` | Requires `enableOfficialMcp`; IaC registers each official MCP server with an APIM-fronted deployment. Preview. See [`../foundry-toolbox.md`](../foundry-toolbox.md) |
| Web IQ search tools | `AI4IA_WEB_SEARCH_ENABLED` | none | `webSearchEnabled` | Web IQ API key or Entra managed identity outside local |
| Session/run tool auto-approval | `AI4IA_TOOL_AUTO_APPROVE_ENABLED` | availability read from API | `toolAutoApproveEnabled` | Default `false`; explicit user consent plus Entra auth and Cosmos outside local. No new Azure resources. |
| Group policy | `AI4IA_GROUP_POLICY_ENABLED`, `AI4IA_GROUP_POLICY_JSON` | current capabilities from API | `groupPolicyEnabled`, `groupPolicyJson` | Default off/unconfigured; bounded operator mapping, validated Entra claims, explicit restrictive defaults. No Graph permissions or membership queries. |
| Reviewed agent/workflow publishing | `AI4IA_ASSET_PUBLISHING_ENABLED` | availability from API | `assetPublishingEnabled` | Default off; group policy, Entra, one tenant, durable Cosmos outside local, explicit owner submission and independent review. |
| Resumable exact-call workflow approvals | `AI4IA_WORKFLOW_APPROVALS_ENABLED` | owner inbox and resumable run controls; availability from API | `workflowApprovalsEnabled` | Default `false`; existing DTS host, metering, finite runtime and approved v1 conversation readiness. Entra + Cosmos outside local. No standing grant or existing-session enrollment. |
| Safe-only workflow scheduling | `AI4IA_WORKFLOW_SCHEDULING_ENABLED` | existing workflow builder; availability from API | `workflowSchedulingEnabled` | Default `false`; requires resumable approvals. Finite once/daily/weekly schedules, current owner/policy checks and explicit no-hard-dollar-cap mode. No new resource or Graph authority. See [workflow automation](../workflow-automation.md). |
| Resumable conversation deletion | `AI4IA_SESSION_DELETION_ENABLED` | owner deletion status / explicit resume | `sessionDeletionEnabled`, `sessionDeletionRolloutId` | Default `false`; new conversations only, approved rollout record selected by `AI4IA_SESSION_DELETION_ROLLOUT_ID`, Entra + single-write-region Cosmos + no-TTL layout. No background cleanup or automatic existing-record enrollment. |
| Admin resource panels | `AI4IA_RESOURCE_METRICS_ENABLED` + resource ids | admin dashboard | resource-id env from modules | Monitoring Reader and ARM resource ids |
| Proxy application profiles | proxy runtime only | none | `proxyProfilesEnabled` | Secret-mounted minimal projection **and verified identity-aware app header**; validator blocks enablement with shared-key ingress |
| Proxy priority reservations | `AI4IA_PROXY_PRIORITIES_ENABLED` | none | `proxyPrioritiesEnabled`, `proxyPriorityWorkers` | Valid `priority:count` reservations; per-replica fairness only. The API and proxy read the **same** switch — see the note below the table |
| Proxy metadata telemetry | proxy runtime only | none | `proxyEventHubTelemetryEnabled` | Creates Event Hubs + proxy sender RBAC only when enabled; no prompt/response/header logging |
| CompanionApp telemetry console | none (not an API feature) | none | `companionAppEnabled` + image, sign-in app, admin set, optional IP ranges and replicas | Default off; creates nothing. Proxy telemetry, an attested digest from `companion-image.yml`, an Entra app registration and at least one admin group or principal. Admin-only and read-only; see [below](#companionapp-telemetry-console) |
| Proxy durable async | proxy runtime only | none | `proxyAsyncEnabled` | Dedicated AVM Blob + Service Bus resources and proxy MI RBAC |
| Raw-file compute (code interpreter) | `AI4IA_CODE_INTERPRETER_RAW_FILES_ENABLED` | none | `codeInterpreterRawFilesEnabled` | Requires document understanding + document compute + a code-interpreter base URL; `api.bicep` emits the env var only when all three hold. Uploads a document's **original bytes** to the sandbox instead of Content Understanding's parsed text, falling back transparently on unsupported/oversize/failed uploads. Had **no Bicep parameter at all** until now, so it was implemented but unreachable from a normal `azd` deploy |
| Azure Monitor alerting baseline | n/a (infra only) | none | `enableAlerts`, `alertEmail` | Action group + api-5xx / Cosmos-429 metric alerts. An action group with **no** receiver is legal ARM and notifies nobody — see the note below |
| Key Vault purge protection | n/a (infra only) | none | `keyVaultPurgeProtection` (`AI4IA_KEYVAULT_PURGE_PROTECTION`) | None — but enabling it is **irreversible**, and it reserves the vault name for the soft-delete retention window, which blocks teardown-and-redeploy of the same environment name. Default `false` for that reason; see the note below |
| Durable workflow execution | `AI4IA_DURABLE_WORKFLOWS_ENABLED` | none | `enableDurableWorkflows`, `durableTaskSkuName`, `durableWorkflowTimeoutSeconds` (`AI4IA_ENABLE_DURABLE_WORKFLOWS`, `AI4IA_DURABLE_TASK_SKU`, `AI4IA_DURABLE_WORKFLOW_TIMEOUT_SECONDS`) | **Provisions a paid Azure resource** (Durable Task Scheduler + task hub); the azd token allows a per-environment opt-out. Also requires `AI4IA_SESSION_STORE=cosmos`, a region that offers `Microsoft.DurableTask`, and that provider registered. See the note below |
| Streamed tool loop | `AI4IA_GATEWAY_STREAM_TOOL_LOOP` | none (read server-side only) | none — API-only setting | None. **Default `true`, i.e. ON**, because OFF is the defect it fixes: a turn that calls a tool would again run every model round trip to completion before emitting anything. It is a kill switch, not a feature gate — it exists so a streaming regression in the one path every chat request takes can be rolled back by an env var instead of a deploy. Off restores the previous wire bytes exactly: the runtime takes the non-streaming `gateway.complete` path and the router emits a single terminal content delta |
| Per-invocation tool approval | `AI4IA_TOOL_APPROVAL_MODE` (`always` \| `tainted` \| `off`) | none (prompt renders from the stream) | none — API-only setting | None. **Default `always`, i.e. ON**; this is the one row in this table that is a security control rather than a feature, so its safe default is *enabled*. See the note below |

**Atomic application admission is not a routine enablement switch.** The source
implements bounded owner-scoped reservations and an existing-usage-partition
Cosmos CAS adapter without a create/upsert path. No state is initialized merely
because an owner authenticates; new sign-ups are refused until an operator
bootstraps them. Activation is gated on an operator-authored
`hard_quota_rollout_v1` record whose evidence the owner has approved. That covers
a rehearsed drain of every non-enforcing replica, the create-only operator
bootstrap of the cohort (including the deploy-canary identity), and the recovery
and retention review. The scope is request-count only. Token/dollar caps refuse
because final usage does not prove all proxy/APIM retry attempts; global default
token/USD caps refuse startup. Group-policy `spend` and execution-actor
`restrictions.spend` limits stay **soft** policy restrictions: they are not
hard-enforced and must not be presented as hard caps. Hard durable workers refuse
rather than replay ambiguously, and the continuous canaries are unavailable. A
rollback to a non-enforcing revision ends the rollout. The local fake is not a
distributed quota. See [the activation contract](../hard-quota-admission.md#request-count-activation-contract).

Numeric soft enforcement remains unchanged. The deliberate exception is that
`AI4IA_ENTITLEMENTS_ENABLED=false` no longer enables an explicitly disabled user:
the known-disabled guard is authoritative in both modes. Existing soft usage
summaries are not the hard admission balance.

**Per-invocation tool approval** is the inverse of every
other row here: leaving it alone is the secure choice, and changing it is what
needs justifying. Gated external/destructive tool calls are held until the user
approves *that call with those exact arguments*, unless an explicit, valid
session/run consent covers the enabled tool. That covers every MCP tool on
both the BYO and official planes, **and** the first-party synthetic capabilities
(`browse_url`, WebIQ searches/suggestions, `run_code`, image/video generation,
`remember_memory`, `export_document`). Marking a server `trusted` or a tool
`requireApproval: never` still decides whether the model is offered the tool; it
no longer decides what leaves the network, because standing trust is precisely
the authority an indirect prompt injection borrows when a document, a memory, a
web result or a previous tool response chooses an outbound call's arguments.

* `always` (default) — gate every external/destructive call.
* `tainted` — gate only when the turn carried untrusted content (session
  documents, recalled memory, library excerpts, or an earlier tool result in the
  same turn). Keeps a trusted server frictionless on turns with no injection
  surface, at the cost of trusting the turn-level taint bit to be complete.
* `off` — restore the pre-approval-control behavior exactly. Not a supported posture for a
  deployment where users register their own MCP servers.

**What a user actually sees under the default.** Three capabilities prompt on
every use: `browse_url`, `run_code`, and `analyze_attachment`. The model chooses
the destination or program for the first two; the third sends attachment bytes
to the external Responses sandbox. Everything else first-party whose destination is fixed by server
configuration — WebIQ searches/suggestions, image/video generation, `remember_memory`,
`export_document` — prompts *only* on a turn that carried untrusted content, so an
ordinary "search the web for X" or "remember that I prefer Y" is not interrupted.
That relaxation is declared per tool (`ToolSpec.injection_only_risk`), not
operator-configurable, and never weakens a call below `tainted` strength.

**Workflow runs require explicit authority, too.** Direct and durable runs no
longer silently opt out through `ApprovalPolicy.off`. A gated call without
run consent fails visibly; the operator can enable the default-off
`AI4IA_TOOL_AUTO_APPROVE_ENABLED` gate so the owner can opt one run in.
The chat `/run_workflow` bridge remains safe-only and never inherits an
unattended run's consent.

**Session/run auto-approval is not an environment-wide bypass.** Setting
`AI4IA_TOOL_AUTO_APPROVE_ENABLED=true` as an azd/repository variable permits the
UI/API opt-in; it does not consent on behalf of any user. Bicep emits the gate to
the API, which validates Entra auth and Cosmos outside local. No resource or RBAC
change is needed. Consent is server-owned, limited to the currently enabled tool
contracts, expiring and revocable. New tools or changed contracts require renewed
consent. Every dispatch still checks ownership, scopes, destinations and budgets,
and keeps activity/receipts with approval provenance. Turning the operator gate
off prevents subsequent auto-approved dispatch, including previously scheduled
runs; it cannot undo an already-running external request.

**Upgrade note:** workflows that previously relied on the blanket unattended
exemption must now explicitly opt in for gated calls. Review the workflow and its
enabled tools before doing so: hostile retrieved content can influence later
calls when per-call prompts are skipped.

The API contracts are owner-scoped:

- `POST /api/sessions/{id}/tool-consent` with `{"enabled": true}` grants consent
  for an existing session; `false` revokes it. It returns the updated session with
  a server-owned `toolConsent` summary (id, scope, grant/expiry timestamps and tool
  count). Generic session PATCH cannot write consent. The lifetime is at most
  eight hours.
- `POST /api/workflows/{name}/run` accepts `autoApproveTools: true` for one run.
  It is not a saved workflow default. Opted-in direct runs and all durable runs
  require an `idempotencyKey`, which gives the caller a run handle before the
  synchronous response is returned. The consent choice is bound to that
  invocation; reusing a direct-run key returns `409` without reexecuting it.
  Durable scheduling retries preserve the original fingerprint.
- `POST /api/workflows/runs/{runId}/cancel` with `{"sessionId": "..."}` revokes
  remaining run authority and requests a stop. It does not undo in-flight
  provider calls; completed and partial receipts remain in the run's messages.
  Poll the status with `?sessionId=...` to include persisted cancellation state.
  A direct run can also be cancelled before its response using
  `POST /api/workflows/{name}/cancel` with `sessionId` and the original
  `idempotencyKey`. A `404` before the run has persisted its claim is not a
  cancellation acknowledgement.

Availability comes from the existing Inspector/tool-catalog and workflow-list
responses as `toolAutoApproveAvailable`. Do not infer it from a frontend env
variable. Receipts distinguish `session`, `run`, `invocation`, `not_required`, and
`operator` approval provenance; `autoApprovedToolCalls` is not the number of
per-call user clicks. Workflows preserve each bounded step receipt separately
from the bounded aggregate (`workflowStepReceipts` on the assistant message).

Approvals are short-lived (10 minutes), single-use, and bound to user, session,
tool and argument digest. "Single-use" is enforced in two independent places,
because they close different holes: the durable record is burned with a
conditional (ETag) write, so two concurrent requests presenting the same grant
cannot both redeem it; and the redeemed authorization is spent the moment it
dispatches one call, so one approval cannot cover a model that emits the same
call repeatedly in a single turn. The one-time grant is delivered once on the
chat SSE stream and is never persisted, so a browser reload intentionally loses
it and the user is asked again rather than silently holding a live capability.
Denying is the absence of a grant: there is no deny endpoint to fail and no state
to unstick.

The approval card never silently shortens itself: per-value length shrinks before
any key is dropped, masked values are labelled as hidden-but-sent rather than
shown as content, and anything that still could not be displayed is counted and
surfaced as a warning on the card. Otherwise a model-chosen argument set could
push the destination of an exfiltration out of view while it still went on the
wire.

### Last observed deployment posture

The following is a **2026-09-06 read-only configuration observation**, separate
from template defaults. Repository overrides, running Container App settings,
ARM resources, and scoped gateway/RBAC metadata were compared. It is not a fresh
end-to-end exercise of every modality, toolbox operation, or WebIQ entitlement.

| Control group | Checked-in profile default | Last observed live posture |
| --- | --- | --- |
| Image/video, document understanding/compute, raw/inline compute, Search, Voice Live + tools, custom tools, Web IQ, summarization, official MCP, Foundry toolbox, private tool catalog | `true`, each through its own `AI4IA_*` binding | Enabled |
| Durable workflows | `${AI4IA_ENABLE_DURABLE_WORKFLOWS=true}` | Enabled |
| Session/run tool auto-approval | `${AI4IA_TOOL_AUTO_APPROVE_ENABLED=false}` | Availability gate enabled; users must still explicitly consent per session/run |
| Proxy priority reservations | `${AI4IA_PROXY_PRIORITIES_ENABLED=false}` | Enabled with `1:2` workers |
| Azure Monitor alerts | `${AI4IA_ENABLE_ALERTS=false}` | Enabled with a recipient |
| Speech Voice Live | `${AI4IA_SPEECH_VOICE_LIVE_ENABLED=false}` | Enabled; allowlist includes `speech_voice_live`, while Azure OpenAI remains the default |
| Proxy profiles, Event Hub telemetry, proxy durable async | `false` | Disabled |

The same observation found 115 model deployments across 48 enabled catalog
entries in exact name/version/SKU/region/capacity parity with the `maximum`
profile; Claude remained off. This is temporal evidence, not a portable quota
allocation. The primary GPT-5.2 Global Standard deployment was 500K TPM, but no
Agentic analyzer id was configured.

Run authenticated direct-FastAPI protocol canaries for both voice providers
after any change. Read the
deployed Container App env when you need the current answer; do not infer it from
the profile default.

## Cross-tenant Claude source contract

**Source-only and default-off.** This contract does not approve provider terms,
directory objects/consent, roles, capacity provisioning, network changes or live
traffic. An unresolved requirement for private networking blocks activation.
The supported source network mode is explicitly **`public-keyless`**, not Private
Link. The existing Basic v2 APIM does not provide outbound VNet integration;
private endpoints, reachable DNS and an appropriate gateway/network design need
separate approval. Do not choose public HTTPS merely to pass this gate.

`infra/models.json` is the only model/version/region/SKU/capacity inventory.
Its `deploymentTarget: external-claude` rows describe one isolated eastus2
account: Opus 5 version 2 at DataZoneStandard 40, and Sonnet 5 version 2 at
GlobalStandard 80 and DataZoneStandard 80. That account was provisioned on
2026-09-25 through `infra/claude-target.bicep`; it also holds Opus 5.5 version 2
at GlobalStandard 40 and DataZoneStandard 40, which stay out of the catalog
until an adaptive-thinking profile exists (see the
[platform evaluation](../foundry-platform-evaluation.md#claude-opus-55)). Opus 5
has no GlobalStandard row because a separately owned deployment in the same
subscription holds that entire quota counter. These are raw model-specific
standard-capacity units, not fixed PTUs, TPM conversions, a dollar reservation
or current headroom. They must be checked again before provisioning. Do not add
Swedish replicas or duplicate versions to multiply them.
Main-stack Bicep always excludes these rows from source accounts. Existing source
allocations, Sora 2, TTS/realtime, memory and document residency floors are unchanged.

The model path remains **API -> existing SimpleL7Proxy -> existing shared APIM
-> exact target Foundry account**. Source system-MI authentication remains
unchanged for ordinary models. The Claude branch obtains a **user-assigned**
managed identity assertion for `api://AzureADTokenExchange`, exchanges it at the
fixed target-tenant OAuth endpoint for `https://ai.azure.com/.default`, and sends
only the target token to the bound `/anthropic/v1/messages` endpoint. Client,
tenant, destination and auth mode are never selected from request headers.
OAuth exchanges have a 10-second timeout, require a bounded Bearer response with
a positive bounded lifetime, and fail with content-free errors. Every Claude
request exchanges against its exact configured tenant/application/audience;
target tokens are **request-local, not cached**. APIM's built-in MI cache retains
only the source UAMI assertion by client/resource. The existing throttle path
already owns the inbound custom-cache lookup; adding a second lookup violates
the [documented policy limit](https://learn.microsoft.com/en-us/azure/api-management/cache-lookup-value-policy).
No target token survives a tenant/client/endpoint configuration change.
This deliberately adds one authentication round trip and a token-endpoint
availability dependency per Claude request. Allocated model capacity is not
evidence of achieved end-to-end throughput or latency.
Source assertions and OAuth responses are not logged, returned or saved in
application receipts. Claude still cannot obtain attempts-v1 or finite/hard-dollar
admission through a generic chat path.

### Separate approved operator units

The following are distinct approvals and execution steps, not actions performed
by the normal application runtime or by this source change:

1. Confirm network intent and the actual legal entity, country and industry for
   **this target subscription**. Existing application settings are not permission
   to reuse an attestation. Version 2 still requires Anthropic Marketplace terms
   through `modelProviderData`; identity-only authentication does not remove them.
2. In the **source tenant**, approve a dedicated UAMI (`infra/claude-identity.bicep`,
   `createIdentity=false` by default) and a dedicated multitenant application in
   that same tenant. Create its single FIC with issuer
   `https://login.microsoftonline.com/<source-tenant>/v2.0`, subject equal to the
   UAMI **principal/object ID**, and audience `api://AzureADTokenExchange`.
   No password/certificate credential or system-MI substitution is accepted.
   Graph creation/consent is an operator step, never a deployment script fallback.
3. Provision that application's service principal in the target tenant after
   the required consent is approved. A target-tenant member without a directory
   role cannot create a service principal for an application registered in
   another tenant. A user-consent policy limited to verified publishers
   (`microsoft-user-default-low`) also keeps an unverified external application
   off the consent path. So an Application Administrator or Cloud Application
   Administrator must run `az ad sp create --id <application-client-id>` or grant
   admin consent. Subscription Owner is not a directory role and does not help.
   Separately approve a **distinct target-reader identity** whose GitHub OIDC
   trust matches the subject `deploy.yml` presents. The job runs under the
   `production` environment, so with the default subject template the subject is
   `repo:<owner>/<repo>:environment:production`. A `ref:refs/heads/main` subject
   alone fails the reader login. The reader must not be the runtime application,
   UAMI or existing source deployment identity. The readers need the exact app,
   FIC and SP metadata reads plus scoped ARM reads used below. A workload identity
   gets those Graph reads only through an admin-consented application permission,
   which is another target-tenant administrator action. Missing Graph access is an
   explicit blocker; do not automatically add broad directory permissions to work
   around it.
4. Prepare an empty dedicated target resource group and the planned binding.
   Run the target preflight below under its isolated reader profile. It reads
   exact version/SKU offerings, their **offered `usageName`** counter, raw remaining
   quota and modelCapacities for the single requested region; it does not normalize
   version-1 counters or sum replicas. Missing/contradictory dates or retirement
   within seven days refuse new provisioning. This read is not a reservation.
5. After separate capacity/terms approval, use `infra/claude-target.bicep` with
   `provisionClaude=true`, explicit `networkMode=public-keyless` and confirmed legal
   parameters. It creates only the dedicated identity-only account and catalog
   deployments; no full application stack, project, key or learning-account
   mutation. The account name is Bicep-derived. Observe its actual outputs/IDs.
6. After separate access approval, `infra/claude-access.bicep`
   (`grantInferenceAccess=false` by default) creates a custom inference role
   with **only** `Microsoft.CognitiveServices/accounts/AIServices/*` data
   actions and an exact-account assignment to the **target SP principal ID**.
   It has no key/secret/control-plane actions. In a 2026-09-25 live check on
   the dedicated account, a principal that never held a broader role got HTTP
   401 with the documented MaaS-only role (`accounts/MaaS/*`) and with
   `AIServices/endpoints/invoke/action` alone; `AIServices/*` authorized Claude
   Messages within about five minutes. Built-in Foundry User and Cognitive
   Services User (`Microsoft.CognitiveServices/*`) are broader and are not
   inference-only substitutes. A separately approved governed canary is still
   required.

### Exact binding and continuously fresh readback

`AI4IA_CLAUDE_BINDING_JSON` contains exactly these noncredential strings. Keep
real values in operator configuration, never source or a public report.

| Binding fields | Required observation |
| --- | --- |
| `sourceTenantId`, `sourceSubscriptionId`, `environment`, `workload` | Exact current source deployment context; no subscription switching |
| `sourceApimResourceId`, `sourceApimPrincipalId` | Existing shared APIM and its unchanged system principal |
| `sourceIdentityResourceId`, `sourceIdentityClientId`, `sourceIdentityPrincipalId` | Dedicated source UAMI; distinguish resource, client and principal IDs |
| `applicationObjectId`, `applicationClientId`, `federatedCredentialName` | Same-source-tenant multitenant app and exact sole UAMI FIC, without secrets |
| `targetTenantId`, `targetSubscriptionId`, `targetResourceGroup`, `targetAccountName` | Dedicated observed target account, never a regional source account or a learning resource |
| `targetPrincipalId`, `targetInferenceRoleDefinitionId` | Target SP bound to the source app; exact custom role definition and account assignment |
| `targetReaderClientId` | Distinct approved read-only identity, not a runtime/deploy credential |
| `networkMode` | Explicit `public-keyless`; absent, private or unknown modes refuse |

The planned binding can identify intended resources for the **pre-creation**
capacity check; it is not evidence they exist. Activation requires actual
observations of every identity/resource/model and policy, not the planned values.
Use an authenticated source CLI profile and an independently authenticated target
reader directory named by `AI4IA_CLAUDE_TARGET_AZURE_CONFIG_DIR`. The helper never
logs in, selects an account, copies a credential cache, follows continuation URLs,
replays a failed read, creates grants, or accepts a saved JSON proof.

```powershell
# Offline configuration shape/scope only; not activation proof.
python scripts/check-claude-binding.py --check
# Before the separately approved target provision (dedicated account group empty).
python scripts/check-claude-binding.py --target-preflight
# After target account/models, app/FIC/SP and role setup; before source provision.
python scripts/check-claude-binding.py
# After source staging or activation; verify real attached identity and APIM routes.
python scripts/check-claude-binding.py --routed
```

The bounded reader allows at most 40 calls, 180 seconds, 1 MiB per response and
8 MiB total. Each process has at most 20 seconds; every CLI request carries the
explicit subscription and fixed profile directory. It verifies exact model,
version, SKU, integer capacity, `Succeeded`, `NoAutoUpgrade`, local-auth disabled,
dedicated ownership, SP/app/FIC and custom-role permissions. The routed check
also reads every referenced current policy fragment, fixed named values and the
non-traceable API-only proxy subscription between stable policy reads. Unknown,
partial, warnings and mismatches are failures; a correct-looking Boolean or
manifest never substitutes for these reads.

APIM's `format=rawxml` readback can change indentation and terminal newlines.
Policy-content comparison therefore ignores only XML comments and whitespace
between elements. Ordered elements, every parsed attribute (including C#
expressions and string literals), and all meaningful body/value text remain
exact. Whitespace inside expressions or payloads is never collapsed. Malformed,
oversized, deeply nested XML, DTDs/entities and processing instructions refuse.
The before/after observations must still be unchanged; semantic comparison does
not excuse a policy update during collection.

Stage source infrastructure with `AI4IA_CLAUDE_EXTERNAL_ENABLED=true` while
`AI4IA_CLAUDE_ENABLED=false`. Main Bicep attaches the preapproved UAMI **alongside**
the system identity, and APIM continues to refuse Claude. `check-model-availability.py`
verifies the external target separately before evaluating source-subscription
models. `postprovision.ps1` requires the routed readback. The deployment workflow
uses an opt-in, isolated target-reader OIDC login with its existing job-scoped
OIDC permission; it performs fresh identity/target reads and repeats route
verification before image rollout, including `provision=false`. Activated CI must
have usable Graph metadata and ARM read authority in both tenants; a successful
operator check from yesterday cannot satisfy it.

Only after target-specific approvals and staged readbacks may the parent/operator
approve setting `AI4IA_CLAUDE_ENABLED=true`, source reconciliation and a bounded
application-path canary. **No live activation or canary is part of this source
contract's completion evidence.** To change a live binding, first disable using
the **old** binding and verify APIM's actual disabled fragment, then change the
binding while disabled. The preflight refuses a changed binding under a live or
unknown auth policy. Roll back advertisement/dispatch, not identity grants or
target data; removing resources/grants requires separate approval.

Single-subscription retirement and capacity reports retain external rows as
unknown and never borrow source inventory/metrics/quota. Production-capacity
selection with enabled external rows refuses unsupported scope. The maximum
planner cannot apply an externally enabled catalog; its source-only plan labels
external coverage unknown. Reader setup cannot claim foreign coverage from a
source-only identity.

### Supported application profile and pricing evidence

Both new models have documented 1M context and 128K synchronous output. This
application deliberately supports **text plus its governed function-tool loop,
thinking disabled, and native `output_config.effort` low/medium/high**. It does
not advertise native vision, adaptive signed-thinking continuation or xhigh/max.
The latter can require thinking and cannot safely round-trip through the current
unified durable history. Missing/contradictory profile metadata refuses; no
provider-default adaptive fallback is used. Historical 4.8 adapter fixtures and
prices remain, without rewriting saved selections or old receipts.

Safe catalog metadata, HTTP parameter validation, adapted payloads, tool
continuations and effective receipt parameters agree on this profile. Publication
and consent bind the full model metadata through their existing environment
digests; ordinary models omit the new default fields to preserve legacy digests.

**Claude Opus 5.5 is deliberately absent (evaluated 2026-09-24).** It is GA in
Foundry, but thinking cannot be disabled: `thinking: {"type": "disabled"}`
returns HTTP 400, and so does forced `tool_choice` (`any` or a named tool). Its
thinking blocks must also round-trip unmodified and are bound to the
conversation prefix. The adapter sends disabled thinking for every
external-Claude profile, so a catalog row would fail every request. Do not rely
on the Learn thinking-table footnote that still marks `disabled` as allowed for
this model. After activation, one approved canary settles that contradiction.
If Anthropic's contract holds, follow the recorded
[adaptive-thinking profile design](../foundry-platform-evaluation.md#adaptive-thinking-profile-design),
which needs an owner decision to amend the thinking-disabled rule.

USD/MTok directional rates are Opus 5 **5 input / 25 output** globally, **5.5 /
27.5** for US DataZoneStandard, and Sonnet 5 **2 / 10** globally and **2.2 / 11**
for US DataZoneStandard. Exact catalog
deployment/SKU, not region alone, selects the rate. Cache reads use the documented
0.1x input rate; cache writes without evidenced duration remain cost-unknown.
The adapter does not request caching. Missing deployment, usage or a lost cache
breakdown is unknown, never free. Cache breakdown is in-process only; no Cosmos
schema or historical repricing is introduced. Shared receipt pricing snapshots
the applicable rates/version before the provider await; monetary/replay activation
gates remain unchanged. These are estimates, not Azure bill guarantees.

Official evidence checked **2026-09-20**:
[Opus 5](https://platform.claude.com/docs/en/models/opus-5/overview),
[Sonnet 5](https://platform.claude.com/docs/en/models/sonnet-5/overview),
[effort](https://platform.claude.com/docs/en/build-with-claude/effort),
[Opus migration/tool-history requirements](https://platform.claude.com/docs/en/models/opus-5/migration-guide),
[Foundry deployment/version hosting](https://platform.claude.com/docs/en/build-with-claude/claude-in-microsoft-foundry),
[pricing and US DataZone multiplier](https://platform.claude.com/docs/en/about-claude/pricing),
[UAMI/application federation](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-config-app-trust-managed-identity),
[APIM managed-identity policy](https://learn.microsoft.com/en-us/azure/api-management/authentication-managed-identity-policy),
[Entra keyless inference roles](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/configure-entra-id)
(its MaaS-only custom role did not authorize Claude Messages live; see step 6),
[APIM networking tiers](https://learn.microsoft.com/en-us/azure/api-management/virtual-network-concepts).
ARM target observations, not public lifecycle prose, remain authoritative for
provisioning admission. Offline .NET expression controls are not an Azure policy
compiler, live authorization proof or private-network test.

## Enablement notes

### Group policy and reviewed publishing

These are source capabilities, not an approved live identity configuration.
Keep both flags off until the operator has reviewed the mapping and named the
independent reviewer identities. Enabling a flag does not create Entra roles,
assign groups, publish existing assets, or migrate user data.

`AI4IA_GROUP_POLICY_JSON` is a strict version-1 object. `domains` can configure
`models` (catalog categories), `zones` (actual inference processing scope),
`tools` (exact canonical governance names), `documents` (read, upload, process,
compute, export, share, annotate, memory, analyzers, index), `publication`
(submit, review, consume), and `admin` (explicit operation names).

Each configured domain requires `default`; it has `allow`, `deny`, and optional
`restrict` lists. Its optional `mappings` list uses an exact `claim` of `roles`
or `groups`, an exact `value`, and the same rule fields. Grants union within
the existing individual/server ceiling, restrictions intersect, and denies win.
Unknown values never become wildcard grants. App roles are the case-sensitive
**claim values**, not directory app-role GUIDs. Groups are exact object IDs.
Malformed, oversized or overage claims cannot restore an unrestricted domain.
There is no Graph lookup or fallback membership inference.

An illustrative mapping structure (the role strings below are examples, not
roles this repository creates or assigns):

```json
{
  "version": 1,
  "domains": {
    "publication": {
      "default": {"allow": ["consume"]},
      "mappings": [
        {"claim": "roles", "value": "example.publisher", "allow": ["submit"]},
        {"claim": "roles", "value": "example.reviewer", "allow": ["review"]}
      ]
    }
  },
  "adminCeiling": []
}
```

`spend` has a `default` limit object and optional claim mappings with `limits`.
The fields match existing entitlements; numeric limits take the minimum of
individual, default and matched restrictions. Usage accounting remains soft:
concurrent work can overshoot, and missing usage/prices are not a bill cap.
Active policy reads distinguish unavailable state from permission. The existing
hard-admission gate, reservations and activation restrictions are separate.

Mapped administration is per operation and additionally intersects
`adminCeiling`, which defaults empty. Existing bootstrap identities and the
configured admin secret remain explicit; group membership does not set a global
administrator flag or proxy priority. Identified usage requires
`admin.directory.read`; entitlement-enriched views require
`admin.entitlements.read`; official MCP refresh is separate from inspection.
A publication reviewer cannot administer entitlements by virtue of review.

Publishing preserves owner/name draft keys and adds conditional revisions.
Owner submission freezes source, audience, model/version constraints,
dependencies and supported tool profiles. A different authorized reviewer
decides that exact snapshot; the freshly authenticated owner then activates it.
Concurrent edits or changed tool/resource/model metadata require a new review.
Only the current active version executes; old receipts and immutable decisions
remain historical. Withdrawal/deletion cannot resurrect through name reuse.

Required contracts cannot disappear. Optional contracts can narrow only for an
explicit supported reason such as an empty selected document scope or a
request-level tool prohibition; discovery failure or unknown metadata is not
such a reason. Receipts and consent bind both the approved profile and actual
subset. `skillMode: "excluded"` is an explicit reviewed no-skill profile;
`"versioned"` requires an explicitly versioned official resource when a loader
is offered. Neither option copies skill bodies, private MCP credentials or
unreviewed private dependencies. Required skills cannot be excluded.

Public means **tenant-visible**, never anonymous. Consumer ownership, current
claims, model/tool permissions, approvals, destinations and budgets are checked
again before dispatch. The publisher's authorization does not transfer.
Current claims mean the current verified token until expiry, not instantaneous
directory revocation. Unattended group-dependent work requires fresh interactive
authorization; queued claims and last-login membership are not authority.
Expiry stops the next protected dispatch, not accounting, status or cleanup
for already accepted work.

Optional `canaryActor` and `evaluationActor` markers each contain exact
`tenantId` and `subject` values and must designate different identities. They
grant nothing. They require a current non-admin, non-publisher model-only policy,
explicit tool/data denial, and applicable numeric limits. Actual model
dispatch additionally requires the real owner-bound one-shot fresh-v1-session
guard with tools and automatic memory prohibited. The monitor is sentinel-only
and bounded to 64 output tokens; authored evaluation has a separate bounded
single-prompt profile and at most 256. Other model/tool/media paths are refused.
`GET /api/execution-capabilities` is a current compatibility observation, never
a bearer grant. Missing profile, policy, v1 readiness or real guard is not ready.

Each execution-actor marker may explicitly opt into a `restrictions` block.
Its required `models` list contains exact categories from the authoritative
model catalog, never deployment names. Its required `spend` object uses the
existing strict entitlement fields and must contain at least one applicable
numeric request/token/cost limit; a compute-only limit is insufficient.
These are existing **soft** limits, not reserved tokens, hard admission or an
Azure bill cap. Numeric values intersect the owner, shared defaults and matched
claim caps by minimum; any disabled flag remains disabled.

The block intersects the current model domain and materializes empty tools and
documents for that exact authenticated tenant/subject/owner only. An omitted
ordinary domain is unrestricted, so narrowing it for the dedicated actor is a
restriction, not a grant. Existing denies, restrict sets, unavailable state and
incomplete claim evidence cannot be removed. Admin/publication domains are not
erased: an underlying administrator or publisher still fails the actor envelope.
Ordinary users retain their existing model, tool, document and privileged
operation behavior. No directory role/group, caller profile selector or writable
user grant field is introduced.

For example, separately approved roleless/no-group monitor and realtime
identities can coexist without restrictive shared defaults. This synthetic
schema illustration is **not an activation configuration or approval of limits**:

```json
{
  "version": 1,
  "canaryActor": {
    "tenantId": "<approved-tenant-guid>",
    "subject": "<dedicated-monitor-service-principal-object-guid>",
    "restrictions": {
      "models": ["chat", "chat-fast"],
      "spend": {"requestsPerMinute": 2}
    }
  },
  "realtimeCanaryActor": {
    "tenantId": "<approved-tenant-guid>",
    "subject": "<distinct-realtime-service-principal-object-guid>",
    "restrictions": {
      "models": ["realtime"],
      "spend": {"requestsPerMinute": 2}
    }
  }
}
```

`evaluationActor` supports the same explicit block but remains a separate
authenticated profile. Omission or `null` preserves the previous behavior and
policy digest: the existing envelope must still be satisfied by ordinary
policy/entitlement composition. Previously incompatible shared-default
configurations do not become compatible automatically. Empty model lists
deny model access; the unchanged envelope refuses an unrestricted catalog.
Unknown categories/fields, malformed blocks and missing applicable limits
fail closed, including configured JSON while policy evaluation is paused.
Enabled actor spend requires both existing soft entitlements and usage metering
at startup. Restrictions participate in current catalog/tool filtering and
configuration digests; an in-flight explicitly restricted actor refuses changed
or removed configuration instead of falling back to ordinary authority.

If current policy becomes malformed or unknown after startup, authentication
still binds the verified owner for canonical session/message reads, accepted-work
accounting and owner-resumed cleanup. The request retains explicit policy
unavailability and any known restricted profile; model/tool catalogs and
protected operations fail unavailable rather than returning a healthy empty
catalog or falling back to ordinary authority. Restoring or pausing policy
cannot authorize protected work in that failed binding; a later authenticated
request must resolve valid current policy. Invalid startup configuration still
fails, and valid actor changes are refreshed even for previously ordinary users.

The distinct optional `realtimeCanaryActor` marker uses the same exact
`tenantId`/`subject` shape but selects only `realtime-setup-canary`. All three
markers must differ. It requires a model domain restricted to `realtime`, empty
tool/document permissions, current non-admin/non-publisher claims and applicable
limits, plus the server's GA selection. Every non-realtime metered surface is
denied. The real factory guard permits one resolved application-relay opening,
one exact setup update, no audio/responses/tools/agent/session context, and only
ordered setup acknowledgements within a 15-second processing deadline that also
covers connection establishment. Current authority is rechecked before frames
are sent or delivered. Socket close and accounting still finish after processing
stops. This is not a provider bill cap.

`GET /api/canary/realtime-capabilities` is the separate setup compatibility
reader; a caller profile/query/header cannot select actor authority. An absent
marker is inert, and pausing group evaluation with a marker still configured
refuses the restricted actor rather than making it an ordinary caller. Keep
explicit policy JSON valid while paused. Before removing an actor configuration
entirely, revoke its API access or disable its individual entitlement and retire
outstanding credentials; an application cannot infer an identity removed from
its configuration after a restart. These are source contracts, not approval to
create actors, change federation, select GA or run a paid canary.

### Resumable conversation deletion

Do not enable this as an ordinary convenience flag. The [deletion
runbook](conversation-deletion.md) defines the separately approved cutover evidence,
minimal retained coordination records, unresolved-upload recovery limits, and
single-write-region/no-TTL startup checks. The template creates no approval record
and merging source starts no reconciler.

The enabled mode refuses unversioned conversations with `migration_required`.
Only conversations created under the new protocol participate. Owners explicitly
resume bounded cleanup; opening status or refreshing it never runs cleanup.
Disabling the gate pauses new protocol work but does not remove v1 access/write
guards, tombstones or fences. Rolling back to binaries that ignore the protocol
is unsafe once v1 data exists. Existing-record migration, production retention
approval and unattended reconciliation remain separate work.

### Applying runtime-contract changes

New image/video gates and the Search-region metrics endpoint are emitted by
Bicep. Release these changes through the normal **provision and deploy** path,
not an API-image-only deployment that skips configuration reconciliation.
The recorded live observation above belongs to its stated deployed commit;
it does not claim these later code/IaC corrections are already live.

Disabling media generation stops new work; it does not authorize deletion of
retained Azure resources or artifacts. An enabled nonlocal media feature with
missing durable storage fails startup rather than silently using a per-replica
artifact store.

### Voice Live

Set:

```text
voiceLiveEnabled=true
voiceLiveToolsEnabled=true        # optional
```

The Origin allowlist is **derived, not configured**: Bicep always folds the
deployed web origins (the Container Apps default FQDN, plus `webCustomDomain`
when one is bound) into `AI4IA_REALTIME_ALLOWED_ORIGINS`, so enabling Voice Live
in a new environment needs no hostname entry and cannot inherit a stale one from
another tenant. Set `AI4IA_REALTIME_ALLOWED_ORIGINS` only to add *extra* origins
(it is union'd with the derived set, never a replacement).

The browser connects directly to the API ingress for `/api/voice/live`; the API
relay validates auth and Origin, resolves the realtime deployment from the model
catalog, and opens the upstream socket through the model gateway. An empty Origin
allowlist is allowed only in local.

The upstream socket is `FastAPI -> APIM -> Foundry`. It does **not** traverse
SimpleL7Proxy because that worker does not support WebSockets. The relay's APIM
subscription is scoped to the realtime API only. The APIM plane must be the
WebSocket-capable Basic v2 service; the retired Consumption SKU did not support
WebSocket APIs at all. Startup fails closed when the `/openai` URL or distinct
realtime subscription key is absent or malformed.

Basic v2 capacity 1 has an approximately $150/month base cost before calls and is
a single-region, single-unit production gateway. It is now the only APIM service in
the environment — the prior Consumption service has been deleted.

### Staged GA Realtime

**Source-ready is not activation.** The GA adapter and conditional APIM objects
ship with `realtimeGaEnabled=false` and `realtimeProtocol=preview`. Model catalog
versions, capacities, deployment counts, the default realtime model and TTS are
unchanged. All live work below requires separate approval under
[`deploy-with-an-agent.md`](../deploy-with-an-agent.md).

The separate [continuous application canary](deployment.md#continuous-application-canaries)
is default-off and cannot enable this gate or select `ga`. Its setup-only
observation requires both the resolved protocol header and ordered server
acknowledgements under separately approved actor/spend scope. It does not replace
the audio, tools, interruption, persistence, cutover or rollback acceptance below.

1. Reconfirm the intended catalog deployment's current offering, entitlement,
   regional capacity and lifecycle evidence. The existing GA `gpt-realtime`
   deployment can be the protocol canary target; staging does not require a
   speculative replacement model. Do not infer a shared or duplicated
   cross-region quota pool from equal availability counters.
2. With approval, stage `AI4IA_REALTIME_GA_ENABLED=true` while leaving
   `AI4IA_REALTIME_PROTOCOL=preview`. Provision and verify the separate
   `openai-realtime-ga` WebSocket API, generated `onHandshake` policy, scoped
   subscription and API secret before updating callers. Existing APIM identity
   and account-scoped role assignments are reused. Check that legacy, model,
   Speech and MCP credentials cannot authorize the GA API and vice versa.
3. Use an approved isolated API environment/revision with
   `AI4IA_REALTIME_PROTOCOL=ga` for a non-sensitive authenticated canary. Confirm
   `/api/voice/live/config` reports `openaiRealtimeProtocol=ga`, then correlate
   the run with the relay completion's `protocol=ga` and the GA APIM handshake.
   The existing voice canary's provider name alone does **not** prove GA coverage:
   both versions use `azure_openai`, and a browser `?protocol=ga` does not select
   it. Exercise microphone/audio and transcript output, session/persona linkage,
   governed tool opt-in and denial, interruption/truncation, close/error cleanup
   and persisted conversation turns. Repeat the unaffected preview and Speech
   controls. Offline fixtures and management-plane reads cannot satisfy this step.
4. Only after approved live evidence and an explicit cutover decision, change
   the intended callers' server selection to `ga`. Keep the preview API, URL/key
   and last known-good application image for a bounded rollback window; record
   owner, canary results, target and rollback conditions.

**Rollback:** set the server selection back to `preview` and deploy the approved
revision, keeping `realtimeGaEnabled=true` while its resources are retained.
Selection is fixed when each connection is resolved: do not retry or replay an
accepted response/tool/audio frame on another protocol. End affected sessions and
start a new connection rather than silently downgrading an active one. Turning
off the staging gate, deleting legacy resources, changing model versions or
reallocating capacity are separate approval decisions, not automatic rollback.

Issue [#413](https://github.com/ian-t-adams/AI4IA/issues/413) remains open until
approved live protocol canaries, cutover/rollback evidence, the realtime-model
lifecycle decision and the separately validated GA TTS migration are delivered.
In particular, the TTS GA version cannot assume spare overlap capacity, and
contradictory public versus subscription realtime-version observations must not
be silently reconciled by editing the catalog. Regenerate projections only with
an approved catalog change. The
[configuration reference](../configuration-reference.md#staged-ga-realtime-protocol)
documents the exact wire boundary and prerequisites.

### Speech Voice Live (second voice provider)

Set:

```text
speechVoiceLiveEnabled=true
voiceProviderAllowlist=azure_openai,speech_voice_live
voiceDefaultProvider=azure_openai        # keep Azure OpenAI default-safe
```

`speechVoiceLiveBaseUrl` and `speechVoiceLiveGatewayApiKey` are wired
module-to-module from the gateway's outputs and never hand-entered; do not set
`AI4IA_SPEECH_VOICE_LIVE_BASE_URL` / `AI4IA_SPEECH_VOICE_LIVE_GATEWAY_API_KEY`
directly. `speechVoiceLiveEnabled=true` requires `voiceLiveEnabled=true` and
`speech_voice_live` present in `voiceProviderAllowlist`; the API refuses to start
with any other combination.

Speech Voice Live routes `Browser -> FastAPI /api/voice/live -> a second,
separately scoped APIM WebSocket API (/speech/voice-live/realtime) on the same
shared active Basic v2 APIM -> the existing eastus2 AIServices account`. It never
traverses SimpleL7Proxy and never adds a new APIM service or Foundry account. The
stable `2026-04-10` catalog allows native-audio `gpt-realtime` (the default) and
`gpt-realtime-mini` with `gpt-4o-transcribe`, plus `gpt-4.1`, `gpt-4.1-mini`,
`gpt-5-mini`, and `gpt-5.1` through the Azure Speech chain with `azure-speech`
transcription. All are initially `eastus2`; only curated
`azure-standard` built-in voices/capabilities from the generated voice provider
catalog are offered, and no custom endpoint, lexicon, or personal voice is
accepted. The shared APIM managed identity additionally needs **Cognitive
Services User** and **Foundry User** (formerly Azure AI User) on that one
account; the `speechVoiceLiveManagedIdentityAudience` parameter (default
`https://ai.azure.com`) is deployment-only, never an app runtime setting.
Verify the selected account accepts this audience by running the authenticated
Speech canary after any account or audience change.

**Enablement status and standing rules.** This provider is **enabled in production**
(`AI4IA_SPEECH_VOICE_LIVE_ENABLED=true`, with `speech_voice_live` in
`AI4IA_VOICE_PROVIDER_ALLOWLIST`). The gates below governed that rollout; the ones
that are standing rules still apply to any future change to this surface:

1. Repository validation passes: catalog/schema checks (including
   `gen-voice-provider-catalog.py --check`), API (`ruff`, `pyright`, `pytest`),
   web (`lint`, `test`, `build`), and IaC/quality gates (schema checks, policy
   tests, `bicep build`, docs drift). These run in CI on every PR.
2. Independent code review, a security review of the WebSocket/secret/event/tool
   surface, and an Azure/Bicep specialist review of the additive APIM/MI/RBAC
   changes.
3. **Standing rule — separate, explicit approval** before running the live APIM
   WebSocket policy compiler (`scripts/test-apim-policy-compiler.ps1`) against the
   target APIM: it creates and deletes temporary Azure resources, so it is never
   run automatically.
4. **Standing rule** — any change touching APIM is reviewed against a zero-delete
   production what-if containing no deletes, replacements, or APIM SKU changes.
5. After deployment, run `scripts/voice-live-canary.py` with an operator Entra
   token against the authenticated FastAPI `wss://.../api/voice/live` path for
   each enabled model, then manually retest a signed-in microphone session,
   provider/model changes on the next connection, and transcript persistence.
   Derive the socket from `AZURE_API_URL`; never use the web/Next.js hostname,
   which cannot proxy WebSockets. Direct APIM handshakes are infrastructure
   diagnostics, not app proof.

Run authenticated canaries against the direct API Container App for both
`speech_voice_live/gpt-realtime` and `azure_openai/gpt-realtime` after enabling
and verify `outcome=success` for each. The signed-in manual microphone retest in step 5 is
the remaining audio/UX validation. Deploying and merging remain separate,
explicitly authorized decisions that this runbook does not grant.

**Rollback and retained resources.** Disabling Speech Voice Live is immediate
and non-destructive:

1. Set `speechVoiceLiveEnabled=false`, or drop `speech_voice_live` from
   `voiceProviderAllowlist` — either alone returns the app to Azure OpenAI-only,
   which remains the default and does not require Speech to be present. With the
   flag `false`, no Speech URL or subscription key is wired into the running API,
   and a fresh deployment does not create the conditional Speech APIM
   API/policy/subscription/named values or Speech-specific Foundry User role
   assignment.
2. For a managed-model regression, narrow the Speech catalog allowlist/default to
   `gpt-realtime`; roll back the API/web revision if needed. v2 browser preferences
   migrate into `ai4ia.voiceLive.prefs.v3`, where Speech's model defaults to
   `gpt-realtime` and remains isolated from the Azure OpenAI deployment choice.
3. ARM Incremental mode does **not** delete a Speech API, operation policy,
   subscription, named values, or deterministic Speech-specific Foundry User
   assignment created by an earlier deployment. The retained API remains
   subscription-key protected and the app has no Speech key, so it cannot call
   the API; nevertheless, the retained objects are dormant privilege and
   inventory. No automatic teardown occurs.
4. Leave those retained objects dormant for diagnosis during an incident. Full
   deactivation is a separate destructive change: refresh the live inventory,
   suspend or revoke `ai4ia-api-speech-voice-live` first, then target only the
   Speech API and operation policy, the `speech-voice-live-wss-endpoint` and
   `speech-voice-live-mi-audience` named values, and the deterministic
   Speech-specific Foundry User role assignment. Review a targeted what-if with
   no unplanned deletes and obtain explicit approval before applying it. Never
   use complete deployment mode on the shared resource group or APIM.

Provider/model/settings changes in the inline selector are persisted separately
and apply only to the next connection; they never reconnect or mutate an active
session. Incident rollback is therefore allowlist/default narrowing plus the prior
app revision, not teardown or deletion of shared APIM/AIServices resources.

### Multi-application gateway controls

Normal HTTP/SSE model calls flow:

```text
application -> SimpleL7Proxy -> APIM -> catalog-selected Foundry deployment
```

App Configuration is always connected with the proxy managed identity. The
postprovision hook reconciles the label-aware `Warm:Sentinel` through the OIDC
deployment identity using Entra authentication and its store-scoped App Configuration
Data Owner role. The proxy keeps only Data Reader; the web and API have no App
Configuration data role. This avoids local credentials and the same-deployment ARM
pass-through RBAC race while keeping bootstrap and refresh real. The proxy applies
only `Warm:Sentinel` and two reviewed request limits from the store, each within a
reviewed range (`Request:DefaultTimeout` 180,000 to 1,200,000 ms,
`Request:DefaultTTLSecs` 300 to 1,200 s); after a sentinel change they refresh on the
configured interval. Every other key, including every `Cold:` key and the
circuit-breaker settings, is refused, and so is an out-of-range value. Those settings
come from the Container App environment and change with a new revision. Making another
setting App Configuration-writable is a reviewed change to the
[App Configuration key policy](../../proxy/README.md#app-configuration-key-policy).

- `proxyPrioritiesEnabled=true` requires `proxyPriorityWorkers` such as
  `1:2,3:1`. Reserved capacity and fairness are in-memory **per replica**.
  This one parameter drives **both** halves of the feature, deliberately: it
  reserves workers on the proxy *and* sets `AI4IA_PROXY_PRIORITIES_ENABLED` on
  the API so FastAPI stamps the `x-S7PPriority` band. Half-enabled is useless in
  either direction — a band with no reservation is inert, and a reservation with
  no band starves, because `simplel7proxy_inbound_post_32.xml` defaults a
  header-less request to the *lowest* band. Bands are `1` high / `2` standard /
  `3` batch, matching `PriorityKeys`/`PriorityValues` in `gateway.bicep`.
  - The band is derived **server-side** from the authenticated principal in
    `ai4ia_api.gateway.priority` and carried in a ContextVar. An inbound
    `x-S7PPriority` from a browser is never read or forwarded; treating one as
    authoritative would let any user claim the reserved workers.
  - Admins (`AI4IA_ADMIN_SUBJECTS` / `AI4IA_ADMIN_EMAILS` / the `admin` app role)
    resolve to band 1; every other authenticated user resolves to band 2. Admin
    membership comes from `ai4ia_api.auth.identity`, the same predicate the
    entitlement API uses, so the two cannot drift.
  - Under spoofable auth (dev provider outside `local`) nobody is promoted:
    identity is client-supplied there, so the feature fails closed.
  - The reservation reaches the proxy as the **`PriorityWorkers`** container env
    var — plural. `ProxyConfig` also declares a singular `PriorityWorker` string
    property, but nothing converts it into `PriorityWorkerDict`, the dictionary
    `WorkerFactory` actually reserves from, so the singular name parses, passes
    validation, and is discarded. `gateway.bicep` emitted the singular name until
    this was measured against the vendored parser: the dict stayed at its
    `2:1,3:1` default, meaning band 1 — the band admins resolve to — got **zero**
    reserved workers while every surface reported the feature as enabled. Pinned
    from both sides by `PriorityWorkerConfigTests.cs` (parser) and
    `test_priority_reservation_uses_the_env_name_the_parser_reads` (bicep).
  - Reserve fewer workers than `Workers` (default 10) in total, or the unreserved
    bands starve. The live value is `1:2` — two workers dedicated to band 1 so
    operators keep capacity when users saturate the app. Band 3 gets no
    reservation on purpose: nothing in this deployment emits it, and the
    remaining workers run as `AnyPriority`, which dequeues the lowest band number
    first and therefore already favours band 1.
  - Unrelated to Azure's paid **Priority Processing** meters, which bill at 2x
    standard. This is queue fairness inside our own proxy and costs nothing.
- `proxyEventHubTelemetryEnabled=true` creates a telemetry namespace and hub and
  sends routing/status/latency metadata to it. Request and response header logging remain false;
  prompts, responses, and profile PII are not emitted. Event Hub is not a queue.
- `proxyAsyncEnabled=true` provisions dedicated Blob + Service Bus resources,
  disables local auth, and grants `id-proxy` only the data-plane roles needed to
  write results and send/receive async jobs.
- `proxyProfilesEnabled` must remain false while the public edge uses the
  temporary shared-key contract. The validator rejects it even when a profile JSON
  is supplied. Enablement requires Entra workload authentication (or another
  verified app-identity boundary); then supply only the minimal server-owned
  Cosmos projection through `AI4IA_PROXY_PROFILE_PROJECTION_JSON`. It is mounted
  as a secret file. Do not configure `UserConfigUrl` to an unauthenticated HTTP or
  Blob URL, and do not grant the proxy Cosmos access.

#### Onboarding another application

Do not share the FastAPI key and trust `X-AI4IA-App-Id`; that would let one
authorized caller impersonate another profile. The supported state in this phase
is one trusted FastAPI application plus disabled profile enforcement.

The onboarding sequence for a future independent application is:

1. provision or register its Entra workload identity;
2. make the proxy edge validate the token and derive an immutable app id;
3. define its allowed model/path set, priority, and quota;
4. publish a minimal server-owned Cosmos projection to the secret snapshot;
5. enable profiles with `UserConfigRequired=true`; and
6. verify unauthorized app ids, models, and paths fail closed before production.

Steps 1-4 are explicit prerequisites, not implemented automation. Until they are
complete, `proxyProfilesEnabled=true` fails validation.

### CompanionApp telemetry console

`AI4IA_COMPANION_APP_ENABLED` hosts a subset of upstream's SimpleL7Proxy
CompanionApp as an admin-only, read-only view of the proxy's Event Hub feed. It is
default off and creates nothing while off.

**What it is.** The console has two pages. The Event Hub monitor shows live
request flow, backends, status codes, latency, requeues and circuit-breaker events.
Insights aggregates the same feed per endpoint and model. The vendored subset is
`proxy/CompanionApp` at the proxy pin.

**What it is not.** Upstream's chat, URL tester, stress, abort, investigator,
vision, history, preferences, App Configuration editor and deployment pages are
not vendored. `proxy/upstream-provenance.json` records each one as
`ai4ia-excluded`, bound to its upstream hash and a reason. Those pages would send
server-side requests to caller-chosen URLs with caller-chosen headers, generate
load and model cost, write shared history, or publish proxy configuration with
the server identity. `AI4IA.CompanionApp.Tests` asserts that the compiled routes
are exactly `/`, `/explore`, `/eventhub`, `/insights`, `/Error` and `/not-found`.
As defense in depth, the injected `HttpClient` refuses every request before
connecting. The console makes no model calls and holds no Foundry, Key Vault,
Storage, Cosmos or App Configuration access.

**Boundary.**

- **Sign-in.** Container Apps authentication requires an Entra session on every
  request, including the Blazor circuit, and redirects anonymous browsers to sign
  in. The built-in authorization policy then admits only the listed admin group or
  principal object ids; everyone else gets 403.
- **In-app gate.** The ARM schema accepts `allowedPrincipals.groups`, but the
  platform documentation only describes `identities` enforcement. So the app
  re-checks every request itself. It reads the `X-MS-CLIENT-PRINCIPAL-ID` and
  `X-MS-CLIENT-PRINCIPAL` headers that Container Apps authentication injects;
  client-supplied copies are dropped. A request passes only if its object id or
  one of its `groups` claims is in the same allowlist. Anything missing, malformed
  or unlisted gets an empty 403 before any page, asset or Blazor circuit runs.
  Group admission requires the app registration to emit **security group claims**;
  a user whose token overflows to group overage is refused, which fails closed.
  Principal ids need no group claim. The check is defense in depth behind Easy
  Auth: it trusts only headers the platform injects on the ingress path and does
  not authenticate anyone by itself.
- **Fail-closed.** An empty admin list would admit every user in the tenant. The
  preprovision validator refuses that configuration. Bicep independently creates
  nothing unless every prerequisite holds, and the app refuses to start with an
  empty or malformed list.
- **Identity.** The dedicated `id-companion-<env>` holds only AcrPull on the
  environment registry and Azure Event Hubs Data Receiver on the one telemetry hub.
  The container pins `AZURE_TOKEN_CREDENTIALS=ManagedIdentityCredential`, and
  startup fails if an Event Hubs connection string or checkpoint store is
  configured. There is no write-capable mode; a configuration editor would need
  App Configuration Data Owner and is out of scope.
- **Ingress.** The Container Apps environment has public ingress and admins have
  no private path to it, so ingress stays external with HTTPS only.
  `AI4IA_COMPANION_APP_ALLOWED_IP_RANGES` optionally adds an IPv4 allow-list; a
  `/0` range is refused. Without one, Easy Auth plus the admin policy is the
  boundary. The app scales to at most one replica, with sticky sessions for Blazor
  circuits.
- **State.** The feed is held in memory, and the console writes no telemetry or
  chat history to disk. Upstream's raw-event `incomplete.json` writer is removed.
  The application files are root-owned, so the app user cannot modify them. The
  only path the app user can write is the ASP.NET Data Protection key ring. It
  lives in the container and is lost with the revision, which only signs users
  out. With `AI4IA_COMPANION_APP_MIN_REPLICAS=0` the console scales to zero, and
  after idle it starts empty from the latest events.

**Cost.** Enabling the console needs `AI4IA_PROXY_EVENTHUB_TELEMETRY_ENABLED=true`,
which provisions a paid Event Hubs Standard namespace. It also adds one small
Container App. Both are owner decisions.

**Image path.** The console is **not an azd service**: azd cannot skip a service
whose app is absent, and the web, api and proxy digest promotion stays unchanged.
Instead:

1. Run the manual, main-only `companion-image.yml` workflow. It builds
   `proxy/CompanionApp.Dockerfile` once, pushes
   `<acr>.azurecr.io/ai4ia/companion-<env>`, gates it on HIGH/CRITICAL findings,
   attests SLSA provenance and an SPDX SBOM, verifies them, and prints the digest
   reference in its summary. It uses the `production` environment and the existing
   deploy identity. It runs in its own concurrency group, because sharing
   deploy.yml's group would let a dispatch replace a queued deploy.
2. Set the `AI4IA_COMPANION_APP_IMAGE` repository variable to that
   `...@sha256:<digest>` reference.
3. Run deploy.yml. Before provisioning, `scripts/verify-companion-image.py`
   re-verifies the attestations for exactly that digest with the pinned GitHub CLI:
   the companion workflow on main, a GitHub-hosted runner, and a single matching
   subject. Only then may Bicep reference it. A tag, a registry-less reference or
   another environment's repository is refused.

Every pull request also builds and scans the image in the `api image` job.

**Enable.** The image lives in this environment's registry, so enable the console
only after the environment has been provisioned once. On a greenfield standup,
the pre-provision gate stops the run because the registry does not exist yet.

1. Enable proxy telemetry: `AI4IA_PROXY_EVENTHUB_TELEMETRY_ENABLED=true`.
2. Register an Entra app for sign-in. Its redirect URI is
   `https://ca-companion-<env>.<environment-default-domain>/.auth/login/aad/callback`.
   The default domain is the same one the other apps use; read it with
   `az containerapp env show -g <rg> -n <environment> --query properties.defaultDomain -o tsv`.
   After provisioning, the exact origin is also emitted as `AZURE_COMPANION_APP_URL`.
   Enable **ID tokens**, set **Assignment required**, and assign only the admin
   group. If you allow-list a group, also set **Token configuration > groups
   claim > Security groups**. No client secret is needed: sign-in uses the
   ID-token flow, and the token store is off.
3. Promote the image as above, then set `AI4IA_COMPANION_APP_ENABLED=true`,
   `AI4IA_COMPANION_APP_IMAGE`, `AI4IA_COMPANION_APP_ENTRA_CLIENT_ID`, and
   `AI4IA_COMPANION_APP_ADMIN_GROUP_IDS` and/or
   `AI4IA_COMPANION_APP_ADMIN_PRINCIPAL_IDS`.
4. Run deploy.yml, then sign in as an admin, and as a non-admin to see the 403.

**Disable.** Set `AI4IA_COMPANION_APP_ENABLED=false` and run deploy.yml. The
incremental provision stops managing the console but does not delete it. Delete
`ca-companion-<env>` and `id-companion-<env>` explicitly, following the teardown
runbook's approval rules. Also remove the identity's two role assignments: a
deleted identity leaves them behind as orphaned assignments.

**Refresh.** Rerun `companion-image.yml` after a proxy refresh or base-image
update, then update `AI4IA_COMPANION_APP_IMAGE`. The console rolls only on
provision.

### Document library and multimodal understanding

Set:

```text
documentUnderstandingEnabled=true
searchEnabled=true
```

Outside local (both `dev` and `prod`), this requires `AI4IA_SESSION_STORE=cosmos`,
`AI4IA_DOCUMENT_BLOB_ACCOUNT_URL`, a configured HTTPS `AI4IA_SEARCH_ENDPOINT`,
and an embedding deployment resolved from `AI4IA_MEMORY_EMBEDDING_MODEL` under
the active catalog/residency policy. The embedding selection remains an API-side
default, not an azd variable. CU is the ingest front door for parsed
Markdown, grounded fields, and media timelines; ready documents feed summary
cards, RAG chunks, `fetch_document`, annotations, save/forget memory, sharing,
and the media player.
The normal azd path derives the Content Understanding endpoint from the primary
Foundry account; `cuBaseUrl` is a direct-Bicep override, not a repository variable.
The Search endpoint is derived from the provisioned service. Both azd
preprovision shells reject library-on/Search-off before model preflight and ARM.
Missing runtime configuration refuses API startup without contacting Search;
in-memory chunks remain a `local` development option only.

**Before adopting this runtime contract:** an environment still relying on
nonlocal in-memory retrieval must obtain approval to enable/provision Search and
confirm embedding resolution, or set `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED=false`
before upgrading. When disabling the library, also disable
`AI4IA_DOCUMENT_COMPUTE_ENABLED` and `AI4IA_CU_PREVIEW_ENABLED` and clear any
`AI4IA_CU_AGENTIC_ANALYZER_ID`. The independent inline-attachment path need not be
disabled. Existing Search-enabled environments retain their tenancy and embedding
configuration. This release does not provision Search, delete indexes, switch
tenancy or rebuild chunks; any necessary derived-index rebuild is a separate
approved operation.

**During a Search outage:** chat reports `library_retrieval_unavailable`, or
`library_retrieval_partial` if some owner-scoped searches succeed. These safe
codes persist in the existing execution receipt's `notes` with `partial=true`
and are visible beside the answer, even if its library prompt block was dropped
for space. Successfully queried zero matches are not an outage. Summary cards,
owned/access-controlled parsed reads and `fetch_document` remain usable when
Cosmos/Blob are healthy; authentication and unrelated plain chat are not
disabled. The Conversation Inspector's document inventory is canonical metadata,
not a live Search query. Semantic-reranker errors still use the same backend's
hybrid fallback and bounded breaker; never resolve a query outage by changing
the tenancy mode or starting an automatic reindex.

Keep rollout work open until configuration compatibility and the approved
rollout's retrieval and source-access behavior are evidenced.

Gaps: the web upload UI is document-centric, custom analyzer authoring is not
surfaced, folder-level sharing is not implemented, and `public` documents remain
tenant-walled rather than anonymous public links.

### Document compute and inline attachment compute

Library compute uses Azure OpenAI Responses API Code Interpreter over ready
library documents:

```text
documentComputeEnabled=true
```

Inline attachment compute reuses the same endpoint/model but is independent of
the library flag:

```text
inlineDocumentComputeEnabled=true
```

Outside local, both fail closed without the dedicated APIM URL, API-scoped key,
and model. The normal azd path creates the isolated API on the existing Basic v2
APIM, targets the primary Foundry account, and fixes the catalogued GPT-5.4 Mini
deployment in policy.

**Both spend an Azure-managed sandbox container per execution, billed per
session rather than per token.** They bypass SimpleL7Proxy but not APIM: the
dedicated policy constrains model/tool/storage posture while equivalent
ownership, approval, and spend controls still run at the call site. Per-user cost
control is the
`computeExecutionsPerDay` entitlement — a rolling 24h cap on sandbox executions,
its own axis because a token or dollar budget cannot express it:

```bash
# Cap a user at 25 sandbox executions per rolling 24h. Omit the field (or DELETE
# the override) to return to unlimited, which is the shipped default.
curl -X PUT "$API/api/admin/entitlements/$INTERNAL_USER_ID" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"computeExecutionsPerDay": 25}'
```

Four properties worth knowing before you set one:

- **It is scoped.** Exhausting it denies `run_code` / `analyze_attachment` and
  nothing else — the user keeps chatting normally. Conversely a chat turn for a
  user whose only limit is this one does no extra ledger read.
- **Both tools share it.** A per-tool allowance would be evaded by asking for the
  other tool; they drive the same sandbox primitive.
- **It charges on attempt.** A sandbox that starts and then fails still cost
  money and still created provider resources, so it still counts. The ledger row
  carries `status: "error"` so the failure is visible in the admin rollups.
- **It can be reached mid-turn.** A turn may perform up to three executions, and
  the check runs before each. The refusal comes back to the model as a tool
  result it can explain, not as a failed turn.

Sandbox spend is reported separately from chat spend: ledger rows carry provider
`azure_openai_code_interpreter` and agent `code_interpreter`, so the admin
by-provider and agents panels break it out. Those rows are deliberately
usage-unknown (the surface reports no tokens) and therefore never priced — an
unknown cost is never rendered as zero. Use `computeExecutions` on the usage
summary, not the token totals, to see how much sandbox was consumed.

Enforcement requires the usage ledger: `AI4IA_ENTITLEMENTS_ENABLED=true` with
`AI4IA_USAGE_METERING_ENABLED=false` is refused at startup, so a limit you set
can never silently fail to apply for lack of a ledger.

### Memory

`AI4IA_MEMORY_STORE` selects and gates the backend:

- `disabled` — off.
- `in_memory` — ephemeral local/dev store.
- `cosmos` — canonical production store with user-partitioned text/vectors, full
  CRUD, ETags, idempotency, and concurrency-safe scoped forgetting.

IaC defaults `memoryStore='cosmos'` from `AI4IA_MEMORY_STORE`. During cutover,
operators deliberately set it to `disabled` to freeze writes before migration,
then restore `cosmos` only after verification. Startup fails closed if the Cosmos
endpoint is missing or either catalog-driven memory model cannot resolve.

The Conversation Inspector exposes a default-on **Automatic memory** switch,
create, inline edit, and confirmed delete. The per-user preference is canonical
state in the existing `memories` container, not a new resource or consent grant.
Missing historical fields default on without a backfill. Off gates automatic
recall, planner writes, and model memory tools at execution time, including
delayed/resumed work; a changed preference generation prevents pre-disable
automatic writes from committing even after reenable. Explicit CRUD, document
save/forget, and scoped forgetting remain available.

Automatic recall and planner consolidation remain best-effort. An unreadable
preference withholds automatic memory, never defaults to enabled; explicit CRUD,
preference updates, and forget operations surface failures. Roll out the
preference-aware API to every replica and durable worker before relying on the
control; an older writer cannot enforce or preserve the new state fields.
Execution receipts identify the memories admitted to a turn, with versions,
hashes, and admitted text. That proves supplied context, not causal influence
on the answer. The collapsed **Memories supplied** view provides focused owner
navigation and identifies unrecorded or bounded evidence without backfilling old
answers. Disabling/deleting memories does not erase historical messages/receipts.
See [Memory architecture](../memory.md).

### Custom photo avatars

Photo avatars generated from a text description are implemented and
default-off. The design, the provider contract and the risks are in
[`../photo-avatars.md`](../photo-avatars.md). Turning the flag on is not
approval to create avatars: the prerequisites below are held outside the
repository, and creation stays refused until the home account itself reports
the Limited Access capability.

**Prerequisites, before any enablement:**

1. **Limited Access approval** for custom text to speech avatar, for the
   subscription that owns the home account, and a registered use case that
   covers AI-generated characters. Keep the approval evidence with the change
   record, never in the repository.
2. **Responsible AI re-approval.** A new modality (synthetic likeness) is a
   trigger-3 change under the [decision record](../rai-decision-record.md). The
   existing annotate-only decision does not cover it until the owner re-approves.
3. **A named owner for the report queue.** Reports land in the owner-partitioned
   `photoAvatars` container, and each one emits a content-free
   `photo_avatar_report` event. Someone must review them and, where the Limited
   Access terms require it, forward them to Microsoft at the report link the API
   returns.

**Enable:**

```text
photoAvatarsEnabled=true
photoAvatarMaxPerUser=5            # optional; 1-50
photoAvatarMaxCreationsPerDay=5    # optional; 1-50
photoAvatarLiveMaxMinutesPerSession=10   # optional; 1-60, live avatar sessions
photoAvatarLiveIdleTimeoutSeconds=120    # optional; 30-900, live avatar sessions
```

`azd provision` then creates:

- the `photoAvatars` Cosmos container (`/userId`, per-item TTL for reports only);
- an `avatars` container on the shared generated-media account;
- the `ai4ia-photo-avatars-v1` APIM API with its six exact operations and generated
  policy, the `photo-avatar-project` named value and an API-scoped subscription;
- the `Host-photoavatars` proxy host holding that subscription's key.

No role assignment is added: APIM's system identity already has Cognitive
Services User on every regional account. Outside local, startup refuses unless
Entra, Cosmos, durable HTTPS Blob and usage metering are configured, and unless
the residency policy is one the catalog home region satisfies.

To limit creation to a pilot group, add an `avatars` domain to the group policy.
Owners outside the group can still list, view, delete and report the avatars they
already have; only creation (`create`) and previews (`use`) need a grant:

```json
{"version": 1, "domains": {"avatars": {
  "default": {"allow": []},
  "mappings": [{"claim": "groups", "value": "<pilot group object id>", "allow": ["create", "use"]}]
}}}
```

**Live checks at enablement.** Run each one once, as a pilot user, and record
the outcome:

1. `GET /api/photo-avatars/config` reports `reason: available`, which means the
   features read returned the catalog's feature name. `capability_unavailable`
   means the approval has not reached the account; stop.
2. Create one avatar. The provider must accept the create, and status must reach
   `ready` with a stored preview. This also proves that APIM's Cognitive
   Services User role can read and create the avatar project and create, read and
   delete avatars. A 403 on any of those stops enablement; a scoped Speech role on
   the home account is a separate owner approval.
3. If a `Succeeded` avatar stays `generating` and the API logs
   `photo avatar preview blocked code=host_not_in_catalog`, the provider issued the
   link from a host other than the catalog's `preview.host`. Nothing was fetched and
   the avatar is not failed: confirm the new host with a read-only observation, and
   change the catalog through review; the next status read then stores the preview.
   `preview_rejected` means the link itself failed a check (shape, a non-public
   address, a redirect, or content that is not a PNG within bounds); stop and
   investigate.
4. Delete the avatar, and confirm the record, the Blob preview and the provider
   avatar are all gone.
5. Confirm the usage ledger holds one known $2 estimate for the create.
6. **Live avatar (Phase 2).** This needs Speech Voice Live enabled. In the Speech
   voice settings, pick the ready avatar and start a signed-in session against
   the direct API Container App socket.
   - The avatar must appear and speak within a few seconds, with the
     `AI-generated` label visible.
   - The `voice_live_completion` log must show `avatar.confirmed=true` and no
     provider id.
   - The usage ledger must hold one `photo_avatar_live` row whose `billableUnits`
     match the connected seconds at $0.60 per minute.
   - Then stay silent past the idle timeout and confirm the session ends with the
     idle notice. This is the first proof through AI4IA's own APIM path.

**Limits and cost.** Each dispatched create is metered once at the catalog
price; an outcome that is still unknown is recorded as cost-unknown, never as
free. A daily creation is spent when the create is dispatched, even if the
provider later rejects it or the avatar is deleted. Creation refuses under any
cost cap if the price is missing. Hard admission covers avatar creation as a
request-only surface; token and dollar caps refuse it.

Live avatar time (Phase 2) bills per second at $0.60 per minute while the session
is connected, idle included.
- Each session is capped by the smaller of `realtime_max_session_seconds` and
  `photoAvatarLiveMaxMinutesPerSession`, and ends after
  `photoAvatarLiveIdleTimeoutSeconds` without conversation.
- It is admitted on its own request-only `avatar_live` surface, which requires the
  `avatar.use` grant, before the voice session's own admission.
- An unpriced live meter refuses under any cost cap.
- Media stays on the existing Voice Live WebSocket (`output_protocol: websocket`):
  there is no WebRTC, no TURN and no new network path to allow.

**Changing the home account.** Each avatar exists only in the account that
created it, and each record keeps that home region. After the catalog's
`homeRegion` changes, APIM routes to the new account, whose answers say nothing
about older avatars. So the API never reads, reconciles or re-verifies those
records, never marks them failed, reports them `usable: false`, and refuses to
delete them with 409 `avatar_home_changed`. Delete every avatar before the change
if you can. Otherwise, removing each one is an operator data change: delete the
provider avatar in the previous home account, then its preview Blob and its record
and ledger entry in the owner's partition, and record the change.

**Degradation and rollback.**

1. If the capability disappears, creation refuses and existing avatars stay
   visible and deletable, with `usable: false`. Nothing is deleted automatically.
2. To stop the feature, first delete any avatars that should not be kept, while
   deletion still works. Then set `photoAvatarsEnabled=false`. The next provision
   removes the API's photo avatar settings, the proxy's `Host-photoavatars` host
   and its `proxy-apim-photo-avatars-key` secret, so nothing in the app can reach
   the avatar API or hold its key.
3. ARM Incremental mode does **not** delete what an earlier provision created:
   the `ai4ia-photo-avatars-v1` API with its six operations and policy, the
   `photo-avatar-project` named value, and the API-scoped
   `<workload>-proxy-photo-avatars` subscription, which stays active with the same
   keys. The `photoAvatars` Cosmos container, the `avatars` Blob container and
   their data remain, and so does every provider avatar in the home account. The
   retained API still requires that subscription's key, which the app no longer
   holds; the retained objects are nevertheless dormant privilege and inventory.
   No automatic teardown occurs.
4. Full deactivation is a separate destructive change: refresh the live
   inventory, suspend or revoke the `<workload>-proxy-photo-avatars` subscription
   first, then target only the photo avatar API with its operations and policy,
   and the `photo-avatar-project` named value. Review a targeted what-if with no
   unplanned deletes, and obtain explicit approval before applying it. Never use
   complete deployment mode on the shared resource group or APIM. Removing the
   Cosmos or Blob container deletes user data: that is a separate data-deletion
   decision.

No user-data deletion or offboarding path exists yet; until one does, remove a
departing user's avatars through the API or by an operator delete of the provider
avatar, the preview and the records in that user's partition.

### Custom MCP tools

Set:

```text
customToolsEnabled=true
```

Outside local, startup requires Cosmos session storage, a Key Vault URI for
durable MCP secrets, and Entra auth. The API applies the SSRF guard, discovers
remote tools, stores credentials in Key Vault, and projects selected tools into
the same governed executor used by built-ins.

### Official MCP plane

A curated, admin-defined set of MCP servers reached **through the shared active
APIM front door** (`infra/modules/apimcore.bicep` owns the Basic v2 service;
`infra/modules/mcpgateway.bicep` owns its MCP children), gated on an
MCP-product-scoped app-global subscription key — distinct from
per-user BYO MCP, which the API calls directly behind the SSRF guard. The bicep
defaults are empty and OFF, but this repo's live parameters enable the plane and
register the portable `ai4ia-toolbox` Foundry toolbox entry. The MCP product contains only MCP APIs, so this key cannot invoke model or realtime APIs.

To register a server and enable the plane:

1. Add an entry to `infra/mcp-servers.json`:

   ```json
   { "name": "ms-learn", "displayName": "Microsoft Learn",
     "description": "Official Microsoft Learn MCP server",
     "upstreamUrl": "https://learn.microsoft.com/api/mcp",
     "upstreamAuthMode": "none" }
   ```

2. Regenerate the packaged runtime catalog (the API image cannot read `infra/`
   at build time):

   ```text
   python scripts/gen-mcp-catalog.py
   ```

3. Set `enableOfficialMcp=true`.

Provision creates/retains the shared `apim-mcp-*` Basic v2 APIM and, when enabled, exposes one governed MCP server per entry at
`https://<mcp-apim>/<name>/mcp`, and wires the gateway URL + subscription key into
the API (`AI4IA_OFFICIAL_MCP_GATEWAY_URL` plus a Container App secret). Startup
fails closed if the plane is enabled without both. Official servers are
admin-curated and marked trusted for discovery/attachment; that standing trust is
**not invocation approval**. Interactive external/destructive calls on both
official and BYO planes still use the exact-argument approval policy described
above unless explicit, current session/run consent covers the enabled contract.
Direct/durable workflows also require authority for gated calls; they do not
inherit an unattended approval bypass.

### Foundry Agent Service toolbox (bridge)

A Foundry **toolbox** is itself an MCP endpoint, so AI4IA consumes it as a single
entry in the official MCP plane above — **no new runtime code, no dedicated app
flag**. This routes the toolbox tools (web/AI search, code interpreter, and tool
search) through the same MCP APIM front door. **Activated in this
repo:** `foundry/toolbox.manifest.json` is the canonical `ai4ia-toolbox` and
`enableOfficialMcp`/`enableFoundryToolbox` are `true`.

To reproduce in a new subscription/environment (full runbook + preview caveats in
[`../foundry-toolbox.md`](../foundry-toolbox.md)), reconcile the ordered data-plane assets
after `azd up`:

1. Verify data-plane access, then provision the toolbox in that environment's Foundry project:

   ```text
   uv pip install -e "app/api[foundry]"
   python scripts/provision-foundry-toolbox.py --check-access
   python scripts/provision-foundry-toolbox.py --create
   ```

2. No per-environment catalog edit needed: the `infra/mcp-servers.json` entry is
   **portable** (`foundryToolbox: true`, no hardcoded URL). `main.bicep` computes
   the toolbox URL from that environment's project endpoint. (Adding a *different*
   toolbox name still means editing the entry + regenerating with
   `python scripts/gen-mcp-catalog.py`.)

3. `enableOfficialMcp` and `enableFoundryToolbox` are already `true` in
   `infra/main.parameters.json`.

`enableFoundryToolbox` grants the MCP APIM managed identity the **"Foundry User"**
role on the project (data-plane scope), so APIM's injected bearer for
`https://ai.azure.com` can invoke the toolbox. `main.bicep` emits the project
endpoint as `AZURE_FOUNDRY_PROJECT_ENDPOINT` for the provisioning scripts. The
bridge still uses the preview `Toolboxes=V1Preview,Skills=V1Preview` contract and
the live toolbox keeps the preview tool-search spelling, although Foundry made
toolboxes (for hosted agents) and tool search GA in September 2026; see the
[Foundry platform updates evaluation](../foundry-platform-evaluation.md#tool-search)
before switching either. The access check requires the
workflow/deployment identity to hold project-scoped Foundry User; Azure OIDC login alone
does not grant data-plane access.

### Private tool catalog (Azure API Center)

`enablePrivateToolCatalog=true` (activated in this repo) provisions an Azure API Center
(`infra/modules/apicenter.bicep`) as a governed inventory of the APIM-fronted MCP
servers. Its region is `apiCenterLocation` (default `eastus`; override via
`AI4IA_API_CENTER_LOCATION`) because API Center is unavailable in some regions,
including `eastus2`. It requires `enableOfficialMcp=true` and has no app-runtime
setting.

Run `azd up`; no follow-up registration command is required. For each official MCP
server, Bicep creates an MCP API, preview version, Streamable HTTP definition, shared
APIM environment, and active deployment whose runtime URI is
`https://<shared-apim>/<name>/mcp`. Definition and environment references use API-Center-scoped
`/workspaces/default/...` IDs. The nonempty deployment principal receives only Azure API Center
Data Reader at the service scope for catalog inspection/reconciliation. API Center MCP inventory
remains public preview. ARM incremental mode does not delete unrelated portal-created samples;
remove `swagger-petstore` only through the exact-ID cleanup procedure in the teardown runbook.

### Web IQ tools

Set:

```text
webSearchEnabled=true
webIqApiKey=<key>
# or AI4IA_WEBIQ_USE_ENTRA=true
```

The API exposes eleven tools to tool-enabled chat and workflow turns:
`web_search`, `news_search`, `video_search`, `image_search`, `browse_url`,
`classic_search`, `finance_search`, `places_search`, `sports_search`,
`sonic_search`, and `web_autosuggest`. Classic covers all 30 documented answer
types, including weather, rather than scraping ordinary web snippets for every
structured answer. Autosuggest is internal beta; all verticals remain subject to
the credential's upstream entitlements.

The client uses fixed v3 REST routes through the official SDK's public
authentication and transport APIs. This preserves documented features that are
not present in every generated SDK resource method, including classic search.
Supported request/response contracts are sourced from the
[SDK reference](https://pypi.org/project/webiq/0.1.6/) and
[WebIQ OpenAPI](https://webiq.microsoft.ai/documentation/openapi.json).
No model traffic or existing gateway routing changes.

Optional azd/repository variables `AI4IA_WEBIQ_BASE_URL`,
`AI4IA_WEB_SEARCH_MAX_RESULTS` (default 5, range 1-50), and
`AI4IA_WEB_SEARCH_MAX_CONTENT_CHARS` (default 6000, range 1-500000) now reach
the API through Bicep. Empty base URL selects `https://api.microsoft.ai/v3`.
The result cap applies to each collection and is additionally clamped by the
provider's endpoint limit; the content cap applies to each content field. All
eleven tools share five calls per turn. Every complete serialized response fits
WebIQ's 8192-byte response limit, including JSON escaping and the nonce fence; the
additional 100000-character turn ceiling never widens this limit. Metadata is
retained before verbose content, and depth/node/list limits bound structured
responses. Truncation is explicit and preserves the outer nonce fence. Strict safe search, credentials,
endpoint choice and retry policy are not model-settable. Automatic retries and
crawl polling remain off, so a single tool invocation cannot silently multiply
outbound calls. `browse_url` rechecks public HTTPS/DNS before each fetch and
requires approval unless valid scoped consent covers it.

Outside local, enabling WebIQ fails closed unless an API key or Entra managed
identity is configured. Configuration does not prove endpoint entitlement.

In CI, `webIqApiKey` is supplied by the `AI4IA_WEBIQ_API_KEY` **`production`
environment secret** (mapped into `.github/workflows/deploy.yml`). If that secret is
empty at provision time, bicep drops the `webiq-api-key` Container App secret and the
api falls back to its managed identity — which must be **entitled to Web IQ**, or
every call returns 401. Set it with `gh secret set AI4IA_WEBIQ_API_KEY --env
production`; see
[`greenfield-standup.md`](greenfield-standup.md#32-secrets).

**Diagnosing failures.** The admin dashboard has a **Web search health** panel
(`GET /api/admin/metrics/web-search`, admin-gated) that reports per-replica call
counts, the auth posture (`authMode`: `api_key` / `managed_identity` /
`unconfigured`), and recent failures by category. The categories are
remediation-oriented: `config` (feature on, no credential), `credential` (a
managed-identity token could not be acquired at all), `auth` (a token was acquired
but Web IQ rejected it — usually the identity is not entitled), `permission`,
`rate_limit`, `timeout` vs `connection`, and `bad_request` / `not_found` /
`server_error` (bucketed from the upstream HTTP status), plus `unknown`. The panel's
headline hint turns `(authMode, recent categories)` into the likely root cause and
fix — e.g. `managed_identity` + `auth` failures means the managed identity is not
entitled to Web IQ, so set the API key secret.

### Azure Monitor alerting baseline

`enableAlerts=true` creates one action group plus two static metric alerts — api
container-app `Requests` filtered to `5xx` (> 10 in 15 min) and Cosmos
`TotalRequests` filtered to `429` (> 10 in 15 min). Both are severity 2, evaluate
every 5 minutes, and auto-mitigate. The module is purely additive: no other
resource depends on it, so turning it on cannot fail an otherwise-good deploy.

**The receiver is the part that silently fails.** An action group with an empty
`emailReceivers` array is valid ARM and deploys clean, so `enableAlerts=true` with
no `alertEmail` gives you alert rules that evaluate, fire, and record in the
portal's Alerts blade while notifying **nobody**. Nothing errors. That is why
`validate-feature-prereqs.py` emits a warning for exactly that combination —
degraded, not broken, so it is deliberately a warning and not a hard failure.

This environment now sets `AI4IA_ALERT_EMAIL=ian@nomad-analytics.com`, a
deliverable mailbox, so the action group has a live email receiver and the
`validate-feature-prereqs.py` warning is clear (the checked-in
`${AI4IA_ALERT_EMAIL=}` default is empty, so a bare local validate still warns).
Do not point it at a non-deliverable `*.onmicrosoft.com` MCAPS owner account —
Graph reports `mail: null` with no Exchange recipient, and a receiver that never
delivers looks configured but notifies nobody.

**The same shape was live one layer down, in the cost budget.** The
resource-group budget (`budget-${workload}-${environmentName}`, $1500/month) is
created unconditionally, but its `budgetAlertEmails` parameter was never
surfaced in `main.parameters.json`. It therefore stayed at its `[]` default and
`cost.bicep`'s `empty(alertEmails) ? {} : ...` produced an **empty notifications
map** — Azure accepted it, the portal rendered a normal-looking budget, and no
threshold could ever email anyone. `main.bicep` now falls back to `alertEmail`
so one address drives both paths, `validate-feature-prereqs.py` warns when
neither is set, and `scripts/tests/test_feature_prereqs.py` locks both halves
in. Set `budgetAlertEmails` explicitly only if budget notices should go
somewhere different from the Monitor action group.

### Key Vault purge protection is off on purpose

`keyvault.bicep` has carried a `purgeProtection` parameter described as "Set true
for production" since it was written, but `main.bicep` never passed it — so no
deployment could turn it on, whatever an operator put in a variable. It is now
reachable via `AI4IA_KEYVAULT_PURGE_PROTECTION`, and it still **defaults to
false**.

That default is a deliberate trade, not an oversight. Purge protection cannot be
switched back off once enabled — Azure offers no path, at any support tier — and
while it is on, a deleted vault's globally-scoped name stays reserved for the
full soft-delete retention window (7 days here). This repo's teardown scripts and
its documented wipe-and-rebuild flow both recreate the environment under the same
name, so enabling purge protection converts a routine rebuild into a week-long
wait for the name to free up.

Turn it on when the environment is genuinely permanent and you have accepted that
you can no longer rebuild it in place. Soft delete — the protection that actually
recovers an accidentally deleted secret — is **always on** regardless, so leaving
this false does not leave secrets unrecoverable; it only leaves the vault itself
purgeable by someone holding the purge permission.

### Durable workflow execution provisions a paid resource

`enableDurableWorkflows` is the only feature flag in this repo that creates a new
**billable Azure resource** when flipped: `infra/modules/durabletask.bicep` stands
up a Durable Task Scheduler plus a task hub. Per AGENTS.md that is a stop-and-ask
change; the `${AI4IA_ENABLE_DURABLE_WORKFLOWS=true}` token is retained
so a second environment can still opt out without a code change.

What it changes when on. `POST /api/workflows/{name}/run` gains an opt-in
`"durable": true`, which returns **202** with a run id instead of executing the
workflow inside the HTTP request; progress is polled from
`GET /api/workflows/runs/{run_id}`. Requests without that field keep running
synchronously on the existing in-request path — the two share one implementation
(`run_workflow_step()` in `workflows/runner.py`).

Sharing that function is necessary but **not sufficient**, and this was learned
the hard way. A durable run reaches it through a serialized orchestration
payload, which is a second place the two paths can diverge: both sides once
hand-listed the fields they carried, so a step's `extraTools` vanished in
transit and a durable step silently ran with different tools than the
byte-identical synchronous step. `build_orchestration_payload` and
`_step_from_dict` therefore use `model_dump(mode="json")` / `model_validate`,
which are exact inverses — any field added to `WorkflowStep` survives by
construction. **Do not reintroduce an explicit field list at that boundary**;
adding a field and forgetting to list it fails no test and raises no error, it
just quietly changes what a durable run executes.

Enabling it in a new environment, in order:

0. Confirm `Microsoft.DurableTask` is registered in the target subscription and
   that the **region supports it** — it is not available everywhere, and the
   scheduler inherits the resource group's location. `azd` deploys run
   `scripts/check-resource-providers.py --register` automatically; for a manual
   provision run it yourself. This is not hypothetical: the provider can be
   `NotRegistered` in a subscription right up until this flag is flipped, because
   a flag-gated module never submits its resource type while the flag is off, so
   nothing had ever caused ARM to register it.
1. Set the repo variables `AI4IA_ENABLE_DURABLE_WORKFLOWS=true` and — only if you
   want something other than the defaults — `AI4IA_DURABLE_TASK_SKU`
   (`Consumption` | `Dedicated`, default `Consumption`) and
   `AI4IA_DURABLE_WORKFLOW_TIMEOUT_SECONDS` (default `1800`).
2. Deploy. Bicep provisions the scheduler and hub, assigns the API's managed
   identity **Durable Task Data Contributor scoped to the task hub** (not the
   scheduler — a second app sharing the scheduler must not be able to read this
   hub's payloads, which carry user prompts and model output), and injects
   `AI4IA_DURABLE_TASK_ENDPOINT` / `AI4IA_DURABLE_TASK_HUB_NAME`.
3. Confirm `AI4IA_SESSION_STORE=cosmos`. `validate_runtime` fails closed if it is
   not: a resumed orchestration can land on any replica, so durable execution
   over an in-memory session store would silently lose state on resume.

Note that steps 2 and 3 fail **closed and loudly**: `validate_runtime` raises if
the endpoint, the hub name, or the Cosmos session store is missing, which stops
the API from starting rather than accepting durable work nothing will execute.
That is the intended trade, but it does mean a half-applied enable is an outage,
not a degraded mode — so do not set `AI4IA_ENABLE_DURABLE_WORKFLOWS=true` without
deploying the infrastructure that supplies the other two values.

Failure posture is deliberate and worth knowing before you page someone:

- Flag **off**: `"durable": true` returns **422**. It never falls back to running
  synchronously, because a silent fallback is indistinguishable from success and
  would hide a misconfigured deploy.
- Flag **on** but the scheduler is unreachable at startup: the app logs the
  failure and keeps serving; durable requests then get the same 422. The feature
  refuses rather than taking the whole API down.
- `scheduler.properties.ipAllowlist` is `0.0.0.0/0`. Container Apps egress IPs are
  dynamic without VNet integration, so any narrower literal list would silently
  lock the API out on the next scale event. The data plane is still Entra-
  authenticated and RBAC-gated at hub scope, so the allowlist is defence in depth
  here, not the primary control.

Payload ceiling: the Durable Task Scheduler caps each JSON-serialized
orchestration payload at **1 MB**. The binding surface is the orchestrator's
return value, which carries *every* step's output at once (the `previous` text
handed to the next step is replaced each time, so it is bounded by a single
result). Six steps of unbounded model output clear 1 MB easily — a reasoning
model can emit well over 100k tokens in one turn — and the SDK would reject the
payload only at the *end* of the run, after all the model work had been paid
for. The orchestrator therefore truncates each step's text to a per-step budget
**derived from `MAX_STEPS`**, with a visible `[truncated: durable run payload
limit]` marker rather than a silent drop.

## Operational reminders

- Network isolation is not an enableable feature posture today. The direct Bicep
  `vnetIsolationEnabled` / `dataTierPrivate` parameters are partial design
  scaffolding, absent from normal azd/CI mapping, and do not cover every required
  Azure service. See the [architecture residual gap](../architecture.md#tradeoffs-and-residual-gaps);
  do not add them to a deployment profile until the endpoint/DNS matrix and cold
  deploy test exist.
- Enabling a feature is a deploy and cost action; validate in an isolated
  environment before changing a live one. A full model catalog may require a
  separate subscription; a reduced same-subscription profile proves only the
  capabilities it retains. See the [teardown validation boundary](./teardown.md#1-validate-iac-without-pretending-subscription-wide-quota-is-duplicable).
- `infra/main.parameters.json` documents this repo's checked-in live posture, not
  the universal defaults.
- If a feature is disabled, its route/service either refuses with 404/disabled
  semantics or is never constructed.
