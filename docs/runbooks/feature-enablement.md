# Runbook: Feature Enablement

Most advanced AI4IA surfaces are implemented but gated. Defaults in code/Bicep are
safe; the live posture is controlled by `infra/main.parameters.json`, azd env
values, and Container App env. Startup validation in
`app/api/src/ai4ia_api/config.py` fails closed for half-wired deployed features.
Use the consolidated parameter/env map in
[`../configuration-reference.md`](../configuration-reference.md) before changing
feature posture.

> **Before enabling a new output modality**, read
> [`../rai-decision-record.md`](../rai-decision-record.md). Every content-safety
> filter on every deployment is **enabled but non-blocking** under an approved
> Azure guardrails-modification exception, and that decision was reasoned about
> *text completions*. Turning on image generation, video generation or a new voice
> provider extends an unfiltered posture to a modality the record did not consider
> — which is trigger 3 in its review-trigger table and requires the record to be
> revisited, not just the flag flipped.

## Flag inventory

| Feature | API flag / setting | Web flag | IaC parameter | Deployed prerequisites |
|---|---|---|---|---|
| Voice Live | `AI4IA_REALTIME_ENABLED` | `VOICE_LIVE_ENABLED` + `API_PUBLIC_URL` | `voiceLiveEnabled` | Browser Origin allowlist outside local |
| Voice Live tools | `AI4IA_REALTIME_TOOLS_ENABLED` | advertised by web env | `voiceLiveToolsEnabled` | Voice Live enabled |
| Speech Voice Live (2nd voice provider) | `AI4IA_SPEECH_VOICE_LIVE_ENABLED` | advertised by web env | `speechVoiceLiveEnabled` | Voice Live enabled; `speech_voice_live` in `AI4IA_VOICE_PROVIDER_ALLOWLIST`; distinct `AI4IA_SPEECH_VOICE_LIVE_BASE_URL` + `AI4IA_SPEECH_VOICE_LIVE_GATEWAY_API_KEY`; separately approved live-validation gate (see below) |
| Document library + multimodal understanding | `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED` | `DOCUMENT_LIBRARY_ENABLED` | `documentUnderstandingEnabled` | Cosmos session store, blob account URL, CU endpoint outside local |
| Library compute / export | `AI4IA_DOCUMENT_COMPUTE_ENABLED` | none | `documentComputeEnabled` | Document understanding, Responses API base URL + model outside local |
| Inline attachment Code Interpreter | `AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED` | none | `inlineDocumentComputeEnabled` | Responses API base URL + model outside local |
| Azure AI Search chunk store | `AI4IA_SEARCH_ENDPOINT` set | none | `searchEnabled` + `searchLocation` | Search service + API identity RBAC |
| Memory / semantic recall | `AI4IA_MEMORY_STORE=cosmos` | inspector create/edit/delete controls | `memoryStore` | Cosmos endpoint/database, vector capability/container, and catalog-resolved embedding/extraction models |
| Rolling conversation summarization | `AI4IA_AUTO_SUMMARIZATION_ENABLED` | none | `autoSummarizationEnabled` | None beyond the active chat model — once the transcript exceeds the model-derived threshold, older turns fold into a running summary while the full transcript stays in storage/scrollback. Off leaves the manual `/summarize` command working but never auto-injects a summary |
| Image generation | `AI4IA_IMAGE_BLOB_ACCOUNT_URL` when provisioned | Settings / imagery UI | `imageGenerationEnabled` | Image-capable model deployment and media blob storage |
| Video generation | `AI4IA_VIDEO_BLOB_ACCOUNT_URL` when provisioned | inline attachment rendering | `videoGenerationEnabled` | Sora-capable deployment and media blob storage |
| Custom MCP tools | `AI4IA_CUSTOM_TOOLS_ENABLED` | `CUSTOM_TOOLS_ENABLED` | `customToolsEnabled` | Cosmos, Key Vault URI, Entra auth outside local |
| Official MCP plane | `AI4IA_OFFICIAL_MCP_ENABLED` | none | `enableOfficialMcp` | MCP-only product/subscription on the shared active Basic v2 APIM + ≥1 server in `infra/mcp-servers.json`; gateway URL + key auto-wired |
| Foundry toolbox (bridge) | consumed via the official MCP plane (no dedicated flag) | none | `enableFoundryToolbox` (+ `enableOfficialMcp`) | Provisioned toolbox in the default Foundry project + a `foundry-toolbox` entry in `infra/mcp-servers.json`; grants APIM MI the project "Foundry User" role. See [`../foundry-toolbox.md`](../foundry-toolbox.md) |
| Private tool catalog (API Center) | admin/IaC only (no app-runtime env) | none | `enablePrivateToolCatalog` | Provisions an Azure API Center to inventory the APIM-fronted MCP servers; asset registration is a documented script step (`scripts/provision-private-tool-catalog.py`). See [`../foundry-toolbox.md`](../foundry-toolbox.md) |
| Web IQ search tools | `AI4IA_WEB_SEARCH_ENABLED` | none | `webSearchEnabled` | Web IQ API key or Entra managed identity outside local |
| Admin resource panels | `AI4IA_RESOURCE_METRICS_ENABLED` + resource ids | admin dashboard | resource-id env from modules | Monitoring Reader and ARM resource ids |
| Proxy application profiles | proxy runtime only | none | `proxyProfilesEnabled` | Secret-mounted minimal projection **and verified identity-aware app header**; validator blocks enablement with shared-key ingress |
| Proxy priority reservations | `AI4IA_PROXY_PRIORITIES_ENABLED` | none | `proxyPrioritiesEnabled`, `proxyPriorityWorkers` | Valid `priority:count` reservations; per-replica fairness only. The API and proxy read the **same** switch — see the note below the table |
| Proxy metadata telemetry | proxy runtime only | none | `proxyEventHubTelemetryEnabled` | Existing Event Hub sender RBAC; no prompt/response/header logging |
| Proxy durable async | proxy runtime only | none | `proxyAsyncEnabled` | Dedicated AVM Blob + Service Bus resources and proxy MI RBAC |
| Raw-file compute (code interpreter) | `AI4IA_CODE_INTERPRETER_RAW_FILES_ENABLED` | none | `codeInterpreterRawFilesEnabled` | Requires document understanding + document compute + a code-interpreter base URL; `api.bicep` emits the env var only when all three hold. Uploads a document's **original bytes** to the sandbox instead of Content Understanding's parsed text, falling back transparently on unsupported/oversize/failed uploads. Had **no Bicep parameter at all** until now, so it was implemented but unreachable from a normal `azd` deploy |
| Azure Monitor alerting baseline | n/a (infra only) | none | `enableAlerts`, `alertEmail` | Action group + api-5xx / Cosmos-429 metric alerts. An action group with **no** receiver is legal ARM and notifies nobody — see the note below |
| Key Vault purge protection | n/a (infra only) | none | `keyVaultPurgeProtection` (`AI4IA_KEYVAULT_PURGE_PROTECTION`) | None — but enabling it is **irreversible**, and it reserves the vault name for the soft-delete retention window, which blocks teardown-and-redeploy of the same environment name. Default `false` for that reason; see the note below |
| Durable workflow execution | `AI4IA_DURABLE_WORKFLOWS_ENABLED` | none | `enableDurableWorkflows`, `durableTaskSkuName`, `durableWorkflowTimeoutSeconds` (`AI4IA_ENABLE_DURABLE_WORKFLOWS`, `AI4IA_DURABLE_TASK_SKU`, `AI4IA_DURABLE_WORKFLOW_TIMEOUT_SECONDS`) | **Provisions a paid Azure resource** (Durable Task Scheduler + task hub). Approved and **enabled 2026-08-02**; the azd token still allows a per-environment opt-out. Also requires `AI4IA_SESSION_STORE=cosmos`, a region that offers `Microsoft.DurableTask`, and that provider registered. See the note below |
| Per-invocation tool approval | `AI4IA_TOOL_APPROVAL_MODE` (`always` \| `tainted` \| `off`) | none (prompt renders from the stream) | none — API-only setting | None. **Default `always`, i.e. ON**; this is the one row in this table that is a security control rather than a feature, so its safe default is *enabled*. See the note below |

**Per-invocation tool approval** (audit finding P1-13) is the inverse of every
other row here: leaving it alone is the secure choice, and changing it is what
needs justifying. Every external/destructive tool call — which today means every
MCP tool on both the BYO and official planes — is held until the user approves
*that call with those exact arguments*. Marking a server `trusted` or a tool
`requireApproval: never` still decides whether the model is offered the tool; it
no longer decides what leaves the network, because standing trust is precisely
the authority an indirect prompt injection borrows when a document, a memory, a
web result or a previous tool response chooses an outbound call's arguments.

* `always` (default) — gate every external/destructive call.
* `tainted` — gate only when the turn carried untrusted content (session
  documents, recalled memory, library excerpts, or an earlier tool result in the
  same turn). Keeps a trusted server frictionless on turns with no injection
  surface, at the cost of trusting the turn-level taint bit to be complete.
* `off` — restore the pre-P1-13 behavior exactly. Not a supported posture for a
  deployment where users register their own MCP servers.

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

The checked-in live parameters currently turn on image/video generation,
document understanding, document compute, inline-attachment code interpreter, raw-file
compute, AI
Search, Voice Live + tools, custom tools, Web IQ search, Cosmos-backed memory,
rolling conversation summarization, and — as of the Foundry activation — the **official MCP plane, the Foundry toolbox
bridge, and the private tool catalog** (`enableOfficialMcp` / `enableFoundryToolbox`
/ `enablePrivateToolCatalog` are all `true`). **Proxy priority reservations and the
alerting baseline are now on too** (`AI4IA_PROXY_PRIORITIES_ENABLED=true` with
`AI4IA_PROXY_PRIORITY_WORKERS=1:2`; `AI4IA_ENABLE_ALERTS=true`), so their
checked-in `false` is a default rather than the live posture. The proxy profile,
Event Hub, and durable-async controls remain `false`; their Bicep defaults are
also off. `speechVoiceLiveEnabled` is the one flag whose checked-in value is a
*default*, not the live posture: `main.parameters.json` carries
`${AI4IA_SPEECH_VOICE_LIVE_ENABLED=false}`, but that repository variable is set to
`true`, so the second provider **is** enabled in the live environment and
`AI4IA_VOICE_PROVIDER_ALLOWLIST` is `azure_openai,speech_voice_live`. Azure OpenAI
Realtime remains the *default* provider (`AI4IA_VOICE_DEFAULT_PROVIDER`); Speech
Voice Live is selectable but still owes the manual microphone canary in
[Speech Voice Live](#speech-voice-live-second-voice-provider) below. Read the
deployed container's env, not this file, when you need the current answer.

## Enablement notes

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

### Speech Voice Live (second voice provider)

Set (only after the gates below close):

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
   Direct APIM handshakes are infrastructure diagnostics, not app proof.

The signed-in manual microphone retest in step 5 is the one outstanding validation
and is tracked in [`roadmap.md`](../roadmap.md). Deploying and merging remain
separate, explicitly authorized decisions that this runbook does not grant.

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

App Configuration is always connected with the proxy managed identity. Warm
settings such as priority reservations and header policy refresh on the configured
interval; Event Hub and async settings are cold and need a revision/restart.

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
- `proxyEventHubTelemetryEnabled=true` sends routing/status/latency metadata to
  the existing telemetry hub. Request and response header logging remain false;
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

### Document library and multimodal understanding

Set:

```text
documentUnderstandingEnabled=true
cuBaseUrl=https://<content-understanding-resource>.cognitiveservices.azure.com
```

Outside local, this also requires `AI4IA_SESSION_STORE=cosmos` and
`AI4IA_DOCUMENT_BLOB_ACCOUNT_URL`. CU is the ingest front door for parsed
Markdown, grounded fields, and media timelines; ready documents feed summary
cards, RAG chunks, `fetch_document`, annotations, save/forget memory, sharing,
and the media player.

Gaps: the web upload UI is document-centric, custom analyzer authoring is not
surfaced, folder-level sharing is not implemented, and `public` documents remain
tenant-walled rather than anonymous public links.

### Document compute and inline attachment compute

Library compute uses Azure OpenAI Responses API Code Interpreter over ready
library documents:

```text
documentComputeEnabled=true
codeInterpreterBaseUrl=https://<resource>.openai.azure.com
codeInterpreterModel=<deployment>
```

Inline attachment compute reuses the same endpoint/model but is independent of
the library flag:

```text
inlineDocumentComputeEnabled=true
```

Outside local, both fail closed without the Responses API base URL and model.

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

The Conversation Inspector exposes create, inline edit, and confirmed delete.
Automatic recall and planner consolidation remain best-effort so a memory service
failure cannot break chat; explicit CRUD and forget operations surface failures.
There is still no global consent toggle or recalled-memory provenance indicator.
See [Memory architecture](../memory.md) and the
[migration runbook](./memory-migration.md).

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
**trusted** (pre-approved, no per-call human gate) and merged ahead of BYO tools
in each turn, sharing one per-turn MCP call budget.

### Foundry Agent Service toolbox (bridge)

A Foundry **toolbox** is itself an MCP endpoint, so AI4IA consumes it as a single
entry in the official MCP plane above — **no new runtime code, no dedicated app
flag**. This routes the whole toolbox (web/AI search, code interpreter, tool
search, and bound skills) through the same MCP APIM front door. **Activated in this
repo:** `foundry/toolbox.manifest.json` is the canonical `ai4ia-toolbox` and
`enableOfficialMcp`/`enableFoundryToolbox` are `true`.

To reproduce in a NEW subscription/environment (full runbook + preview caveats in
[`../foundry-toolbox.md`](../foundry-toolbox.md)) — it is **`azd up` + one command**:

1. Provision the toolbox (and any skills) in that environment's Foundry project:

   ```text
   uv pip install -e "app/api[foundry]"
   python scripts/provision-foundry-skills.py --create      # optional
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
endpoint as `AZURE_FOUNDRY_PROJECT_ENDPOINT` for the provisioning scripts. All
toolbox/skills/tool-search features are **public preview**.

### Private tool catalog (Azure API Center)

`enablePrivateToolCatalog=true` (activated in this repo) provisions an Azure API Center
(`infra/modules/apicenter.bicep`) to act as a governed inventory of the
APIM-fronted MCP servers. Its region is `apiCenterLocation` (default `eastus`;
override via `AI4IA_API_CENTER_LOCATION`) because API Center is not available in
every region (notably not `eastus2`). It is independent of the MCP plane flags --
it catalogs whatever exists -- and has **no app-runtime impact** (admin/IaC concern only).

1. Set `enablePrivateToolCatalog=true` and `azd up`. `main.bicep` emits the service
   name as `AZURE_API_CENTER_NAME`.
2. Register the servers as MCP assets (a preview, script-driven step; not baked into
   Bicep):

   ```bash
   # dry run
   python scripts/provision-private-tool-catalog.py \
     --api-center "$AZURE_API_CENTER_NAME" --gateway-url "$AZURE_OFFICIAL_MCP_GATEWAY_URL"
   # register via SDK (needs the `foundry` extra)
   python scripts/provision-private-tool-catalog.py --create \
     --api-center "$AZURE_API_CENTER_NAME" --gateway-url "$AZURE_OFFICIAL_MCP_GATEWAY_URL" \
     --resource-group "$AZURE_RESOURCE_GROUP" --subscription-id "$AZURE_SUBSCRIPTION_ID"
   ```

The script catalogs each server's **APIM consumer URL**
(`https://<mcp-apim-gateway>/<name>/mcp`), so discovery stays on the proxy and the
catalog integrates with Microsoft Foundry private tool catalogs. API Center MCP
asset registration is **public preview**.

### Web IQ tools

Set:

```text
webSearchEnabled=true
webIqApiKey=<key>
# or AI4IA_WEBIQ_USE_ENTRA=true
```

The API exposes five tools to tool-enabled turns: `web_search`, `news_search`,
`video_search`, `image_search`, and `browse_url`. It sanitizes and nonce-fences
returned content and caps per-turn search fan-out. Outside local it fails closed
unless an API key or Entra managed identity is configured.

In CI, `webIqApiKey` is supplied by the `AI4IA_WEBIQ_API_KEY` **`production`
environment secret** (mapped into `.github/workflows/deploy.yml`). If that secret is
empty at provision time, bicep drops the `webiq-api-key` Container App secret and the
api falls back to its managed identity — which must be **entitled to Web IQ**, or
every call returns 401. Set it with `gh secret set AI4IA_WEBIQ_API_KEY --env
production`; see [`deployment.md`](deployment.md#26-web-iq-api-key-secret).

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
change; it was approved and **enabled on 2026-08-02**, so a scheduler is
provisioned today. The `${AI4IA_ENABLE_DURABLE_WORKFLOWS=true}` token is retained
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
   provision run it yourself. This is not hypothetical: the provider was
   `NotRegistered` in `sub-planetexpress-slurmfactory` right up until this flag
   was flipped, because a flag-gated module never submits its resource type while
   the flag is off, so nothing had ever caused ARM to register it.
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

- Enabling a feature is a deploy and cost action; validate in a parallel resource
  group before changing a live environment.
- `infra/main.parameters.json` documents this repo's checked-in live posture, not
  the universal defaults.
- If a feature is disabled, its route/service either refuses with 404/disabled
  semantics or is never constructed.
