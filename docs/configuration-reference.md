# Configuration Reference

The Conversation Inspector and its session/tool/memory/usage endpoints require no
new feature flag. They expose only capabilities already enabled by the authoritative
API settings below; disabled library, memory, voice, or telemetry sources return an
explicit disabled/unavailable state.

## Admin operations queries

| API setting / environment | Source | Purpose |
|---|---|---|
| `log_analytics_workspace_id` / `AI4IA_LOG_ANALYTICS_WORKSPACE_ID` | Existing workspace `customerId` output | Managed-identity `LogsQueryClient` target for fixed admin KQL |
| `log_analytics_workspace_resource_id` / `AI4IA_LOG_ANALYTICS_WORKSPACE_RESOURCE_ID` | Existing workspace ARM id | Azure Portal diagnostics link only |

`infra/modules/monitoring.bicep` exports the existing workspace customer id and
`infra/modules/api.bicep` supplies both values to the API Container App. No workspace
or RBAC resource is added. The existing subscription-scope Monitoring Reader posture
is reused; if it cannot query the workspace, panels return `unavailable`.
The operations queries consume metadata-only custom events in the existing
workspace `AppEvents` table. No additional workspace, diagnostic setting, or role
assignment is created.

AI4IA has too many knobs to leave them scattered across Bicep, azd, Container
App env, and README prose. This page is the operator map: set the azd value,
watch the Bicep parameter, and know which runtime setting appears in the app.

## Required deployment ownership values

| Purpose | azd / CI variable | Bicep parameter | Notes |
| --- | --- | --- | --- |
| Deployment posture | `AI4IA_APP_ENVIRONMENT` | `appEnvironment` | `dev` or `prod`; **defaults to `dev`**. Set it to `prod` for any real environment — a standup that only sets the *documented* flags deploys as `dev` and nothing fails. Reaches both the api and web container apps; `/health/ready` echoes it back, which is the cheapest way to confirm it landed. |
| Accountable owner tag | `AI4IA_OWNER` | `owner` | Do not leave this as a personal fork default. It lands on resource tags. |
| Cost center tag | `AI4IA_COST_CENTER` | `costCenter` | Defaults to `genai-demo`; override for real chargeback. |
| APIM publisher email | `AI4IA_APIM_PUBLISHER_EMAIL` | `apimPublisherEmail` | Required by API Management. Use an operator-owned mailbox, not a person. |
| Budget alert recipients | Bicep override only today | `budgetAlertEmails` | Empty means budget tracking without email notifications. |
| Entitlement admin shared secret | `AI4IA_ADMIN_API_SECRET` | `adminApiSecret` (`@secure()`) | Optional; empty by default. Only meaningful under spoofable dev auth — empty means identity-only admin, which is the fail-closed posture under Entra. When set it flows to the api as the `admin-api-secret` Container App secret. Leave unset in a production tenant. |
| Speech Voice Live MI audience | `AI4IA_SPEECH_VOICE_LIVE_MI_AUDIENCE` | `speechVoiceLiveManagedIdentityAudience` | Defaults to `https://ai.azure.com`, the audience the `azure-ai-voicelive` SDK requests for the api-version this stack pins. Surfaced as the APIM named value `speech-voice-live-mi-audience` so a future api-version can move it without a code change. Never caller-influenced; leave at the default unless Azure changes the audience. |

## Naming tokens (subscription / tenant portability)

These are **not** env vars or Bicep parameters — they live in `infra/models.json` under `naming`,
the single source of truth read by Bicep, the catalog scripts, and the app runtime. Change them
(and regenerate the model catalog) when standing the stack up in a new subscription/tenant. Full
procedure: [`runbooks/deployment.md` §3](runbooks/deployment.md#3-moving-to-a-new-subscription-or-tenant-11-standup).

| Token | `infra/models.json` path | What it names |
| --- | --- | --- |
| Subscription token | `naming.subscriptionToken` (default `slurmfactory`) | Every model deployment: `{model}-<token>-<region>-<sku>`. Must match between Bicep (creates the deployments) and the runtime catalog (routes to them) — a single edit + `scripts/gen-model-catalog.py` keeps them in sync. |
| Foundry token | `naming.foundryToken` (default `aiforia`) | Foundry accounts/projects (`mf-<token>-<env>-<region>`) and the computed toolbox MCP URL. |

## Feature flags and prerequisites

A flag is set one of two ways, and they are not interchangeable.

- **azd / CI variable** — a repo variable that `deploy.yml` forwards into the azd
  environment and `infra/main.parameters.json` reads with a `${VAR=default}`
  token. Both halves are required. Set it in repo settings; no code change.
- **checked-in parameter** — a literal value in `infra/main.parameters.json`.
  Changing it is a pull request, not a repo-variable edit.

The distinction matters because getting it wrong is silent: setting a repo
variable that no `${…}` token reads leaves the deploy green and the value
ignored. The **Runtime setting emitted** column below is what Bicep writes into
the container — those names are *outputs*, not knobs you set.

| Feature | azd / CI variable | Bicep parameter | Runtime setting emitted | Required companion config |
| --- | --- | --- | --- | --- |
| Voice Live | checked-in parameter | `voiceLiveEnabled` | `AI4IA_REALTIME_ENABLED`, `VOICE_LIVE_ENABLED`, `API_PUBLIC_URL`, `AI4IA_REALTIME_ALLOWED_ORIGINS` | None. The Origin allowlist is derived in Bicep from the deployed web origins (ACA default FQDN + `webCustomDomain`); `AI4IA_REALTIME_ALLOWED_ORIGINS` is optional and only *adds* origins. |
| Voice Live tools | checked-in parameter | `voiceLiveToolsEnabled` | `AI4IA_REALTIME_TOOLS_ENABLED`, `VOICE_LIVE_TOOLS_ENABLED` | Requires Voice Live. |
| Speech Voice Live (second voice provider) | checked-in parameter | `speechVoiceLiveEnabled` | `AI4IA_SPEECH_VOICE_LIVE_ENABLED` | Requires `AI4IA_REALTIME_ENABLED=true`, `AI4IA_VOICE_PROVIDER_ALLOWLIST` to include `speech_voice_live`, and both `AI4IA_SPEECH_VOICE_LIVE_BASE_URL` + `AI4IA_SPEECH_VOICE_LIVE_GATEWAY_API_KEY`. The six managed models and default are catalog-controlled. Bicep default OFF; **enabled** in `infra/main.parameters.json` and live in this deployment. A narrower gate still applies to the managed-identity *audience* default — see [Speech Voice Live](#speech-voice-live-second-voice-provider) — but it does not gate enablement. |
| Voice provider allowlist / default | n/a (server-authoritative) | `voiceProviderAllowlist`, `voiceDefaultProvider` | `AI4IA_VOICE_PROVIDER_ALLOWLIST` (default `azure_openai`), `AI4IA_VOICE_DEFAULT_PROVIDER` (default `azure_openai`) | Allowlist must always include `azure_openai`; default provider must be an allowlist member. The browser may only select an advertised, allowlisted provider. |
| Document library / Content Understanding | checked-in parameter | `documentUnderstandingEnabled` | `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED`, `DOCUMENT_LIBRARY_ENABLED` | Cosmos + blob storage; CU endpoint defaults to the primary Foundry endpoint unless overridden. |
| Library compute / export | checked-in parameter | `documentComputeEnabled` | `AI4IA_DOCUMENT_COMPUTE_ENABLED` | Requires document understanding. Code Interpreter endpoint/model default to primary Foundry + `gpt-5.4-mini-*` unless overridden. |
| Inline attachment compute | checked-in parameter | `inlineDocumentComputeEnabled` | `AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED` | Uses the same Code Interpreter endpoint/model as library compute. |
| Raw-file compute | checked-in parameter | `codeInterpreterRawFilesEnabled` | `AI4IA_CODE_INTERPRETER_RAW_FILES_ENABLED` (Bicep default `false`; **enabled** in `main.parameters.json`), `AI4IA_CODE_INTERPRETER_MAX_RAW_FILE_BYTES` (default 25 MiB) | Requires library compute (`documentUnderstandingEnabled` + `documentComputeEnabled` + a code-interpreter base URL; `api.bicep` emits the env var only when all of those hold). When off, `run_code` sees only Content Understanding's **parsed text**; when on it uploads the document's **original bytes** to the sandbox so the model reads the real PDF/xlsx/csv. Falls back to parsed text on any unsupported type, oversize original, or upload failure, so enabling it cannot break an existing run. |
| Azure AI Search | `AI4IA_SEARCH_LOCATION` | `searchEnabled`, `searchLocation` | `AI4IA_SEARCH_ENDPOINT` when provisioned | Region must have Search SKU capacity. |
| Durable workflow execution | `AI4IA_ENABLE_DURABLE_WORKFLOWS` | `enableDurableWorkflows`, `durableTaskSkuName`, `durableWorkflowTimeoutSeconds` | `AI4IA_DURABLE_WORKFLOWS_ENABLED`, `AI4IA_DURABLE_TASK_ENDPOINT`, `AI4IA_DURABLE_TASK_HUB_NAME`, `AI4IA_DURABLE_WORKFLOW_TIMEOUT_SECONDS` (default 1800) | **Provisions a paid Azure resource** (Durable Task Scheduler + task hub), so enabling it needs an approved deploy; approved and **on since 2026-08-02**, with the azd token retained for per-environment opt-out. Requires `AI4IA_SESSION_STORE=cosmos` — durability without shared storage is theatre, since a resumed orchestration on another replica must see the same session state. Both the endpoint and hub name are required outside `local`; `validate_runtime` fails closed if either is missing. When off, `POST /api/workflows/{name}/run` with `"durable": true` returns **422**, never a silent synchronous fallback. |
| Image generation | checked-in parameter | `imageGenerationEnabled` | `AI4IA_IMAGE_BLOB_ACCOUNT_URL` when provisioned | Image-capable model deployment and generated-media storage. |
| Video generation | checked-in parameter | `videoGenerationEnabled` | `AI4IA_VIDEO_BLOB_ACCOUNT_URL` when provisioned | Video-capable model deployment and generated-media storage. |
| Custom MCP tools | checked-in parameter | `customToolsEnabled` | `AI4IA_CUSTOM_TOOLS_ENABLED`, `CUSTOM_TOOLS_ENABLED` | Cosmos + Key Vault; Entra auth outside local/dev. |
| Official MCP plane (APIM-fronted) | checked-in parameter | `enableOfficialMcp` | `AI4IA_OFFICIAL_MCP_ENABLED`, `AI4IA_OFFICIAL_MCP_GATEWAY_URL`, `AI4IA_OFFICIAL_MCP_SUBSCRIPTION_KEY` (secret) | MCP-only product and subscription on the shared active Basic v2 APIM + at least one entry in `infra/mcp-servers.json`. Gateway URL + key are wired module→module; enabled-without-both fails closed at startup. |
| Foundry toolbox (bridge) | checked-in parameter | `enableFoundryToolbox` (+ `enableOfficialMcp`) | consumed via the official MCP plane; `AZURE_FOUNDRY_PROJECT_ENDPOINT` output feeds `scripts/provision-foundry-*.py` | Toolbox is consumed as one `infra/mcp-servers.json` entry (no app runtime setting). Flag grants the MCP APIM MI the project "Foundry User" role. Provision the toolbox first; preview. See [`foundry-toolbox.md`](foundry-toolbox.md). |
| Private tool catalog (API Center) | `AI4IA_API_CENTER_LOCATION` | `enablePrivateToolCatalog`, `apiCenterLocation` | `AZURE_API_CENTER_NAME` output feeds `scripts/provision-private-tool-catalog.py` | Provisions an Azure API Center to inventory the APIM-fronted MCP servers (admin/IaC only, no app runtime setting). `apiCenterLocation` defaults to `eastus` because API Center is unavailable in some regions (notably `eastus2`). Asset registration is a documented script step; preview. See [`foundry-toolbox.md`](foundry-toolbox.md). |
| Web IQ search tools | `AI4IA_WEBIQ_API_KEY` (secret) | `webSearchEnabled`, `webIqApiKey` | `AI4IA_WEB_SEARCH_ENABLED`, `AI4IA_WEBIQ_API_KEY` (secret) | API key **or** an Entra managed identity entitled to Web IQ. In CI the key is a `production` environment secret; if unset, the api falls back to the managed identity and calls 401 unless it is entitled. Diagnose with the admin **Web search health** panel. |
| Memory / semantic recall | `AI4IA_MEMORY_STORE` | `memoryStore` (default `cosmos`) | `AI4IA_MEMORY_STORE` | Use `disabled` for the migration freeze and `cosmos` after verification. Cosmos requires its endpoint/database, `EnableNoSQLVectorSearch`, the `/userId`-partitioned `memories` container, and catalog entries for the embedding and extraction models. |
| Rolling conversation summarization | checked-in parameter | `autoSummarizationEnabled` | `AI4IA_AUTO_SUMMARIZATION_ENABLED` | None beyond the active chat model. Off sends the full transcript and leaves manual `/summarize` working; on folds the oldest turns into a running summary once the transcript passes the model-derived threshold, keeping the newest turns verbatim and the full transcript in storage. |
| Legacy memory source / document-index fallback | `AI4IA_POSTGRES_LOCATION` | `postgresLocation` | `AI4IA_POSTGRES_*` while retained | PostgreSQL is not a memory backend. It remains temporarily for migration and as the document-chunk fallback when Azure AI Search is absent. |
| Proxy application profiles | `AI4IA_PROXY_PROFILES_ENABLED`, `AI4IA_PROXY_PROFILE_PROJECTION_JSON` (secret) | `proxyProfilesEnabled`, `proxyProfileProjectionJson` | `UseProfiles`, `UserConfigRequired`, secret-mounted `file:/mnt/ai4ia-profiles/profiles.json` | **Blocked by validation while shared-key ingress is used.** Requires a verified identity-aware application header; no public/unauthenticated profile URL is permitted. |
| Proxy priority reservations | `AI4IA_PROXY_PRIORITIES_ENABLED`, `AI4IA_PROXY_PRIORITY_WORKERS` | `proxyPrioritiesEnabled`, `proxyPriorityWorkers` | `PriorityWorkers`, `PriorityKeys`, `PriorityValues`; API emits `x-S7PPriority` | Worker map must use `priority:count` pairs. Off means no reserved workers and no band header. Band is derived server-side from the authenticated principal (admins -> 1, other users -> 2); an inbound `x-S7PPriority` is never trusted. Queue fairness is per replica. Not Azure paid Priority Processing. |
| Proxy Event Hub telemetry | `AI4IA_PROXY_EVENTHUB_TELEMETRY_ENABLED` | `proxyEventHubTelemetryEnabled` | `EVENTHUB_NAMESPACE`, `EVENTHUB_NAME`, `EVENT_LOGGERS=eventhub` | Metadata only: header/body logging stays disabled. Event Hub is telemetry, not the synchronous queue. |
| Proxy durable async | `AI4IA_PROXY_ASYNC_ENABLED` | `proxyAsyncEnabled` | `AsyncModeEnabled`, MI-only Blob/Service Bus config | Creates dedicated default-off AVM Storage + Service Bus resources and grants only Blob Contributor plus Service Bus Sender/Receiver to `id-proxy`. |
| Proxy capacity | `AI4IA_PROXY_WORKERS`, `AI4IA_PROXY_MIN_REPLICAS`, `AI4IA_PROXY_MAX_REPLICAS` | `proxyWorkers`, `proxyMinReplicas`, `proxyMaxReplicas` | `Workers` + Container App scale | Minimum replicas cannot be zero on the active model path. More replicas increase capacity but split in-memory fairness state. |
| Proxy App Configuration label | `AI4IA_PROXY_APPCONFIG_LABEL` | `proxyAppConfigLabel` | `AZURE_APPCONFIG_ENDPOINT`, label, 30-second refresh | Warm settings (profiles, priorities, header policy) reload; cold settings (Event Hub/async) require a new revision or restart. |

## Custom domains

| Host | azd / CI variable | Bicep parameter | Failure mode |
| --- | --- | --- | --- |
| Web app vanity host | `AI4IA_WEB_CUSTOM_DOMAIN` | `webCustomDomain` | Empty value removes the custom domain binding on provision. |
| Existing web managed certificate | `AI4IA_WEB_MANAGED_CERT_NAME` | `webManagedCertName` | Empty value derives a cert name and can create/adopt a different cert. |
| Proxy vanity host | `AI4IA_PROXY_CUSTOM_DOMAIN` | `proxyCustomDomain` | Empty value removes the custom domain binding on provision. |
| Existing proxy managed certificate | `AI4IA_PROXY_MANAGED_CERT_NAME` | `proxyManagedCertName` | Empty value derives a cert name and can create/adopt a different cert. |

If a vanity hostname matters, set the domain variables before running
`azd provision`. Pretending this is optional is how you get a green deployment
and a dead vanity URL.

## Model gateway direction and trust

Normal HTTP/SSE traffic uses
`AI4IA_MODEL_GATEWAY_URL=https://<proxy>/openai`. The proxy's only backend is
`https://<apim>/openai`; APIM terminates at Foundry endpoints generated from
`infra/models.json`. Ordered, size-bounded policy fragments initialize the
catalog and routing setup; they rewrite region-specific deployment names and
reject unknown deployments rather than falling back.

Per-model `reasoning_effort` is also catalog-driven. The API only exposes and
forwards values listed in `infra/models.json` `reasoningEffort`; APIM now treats
unsupported-value 400s as permanent provider errors and returns the upstream
diagnostic body instead of requeueing or parking healthy backends.

The generated API policy is a sub-16 KB wrapper over bounded endpoint and
priority-section fragments. APIM block expressions misparse contiguous `://`
inside string literals, so generated `@{ ... }` expressions split URL slash
tokens.

`infra/policies/simplel7proxy-rollback-policy.xml` preserves the current minimal
live API policy as an explicit operator rollback target. Bicep never deploys it
automatically.

Voice Live uses `AI4IA_REALTIME_BASE_URL=https://<shared-active-apim>/openai` and
the separate secret `AI4IA_REALTIME_GATEWAY_API_KEY` from the FastAPI relay. The
shared active API is a WebSocket API and its subscription is scoped only to
`/openai/realtime`, so it cannot bypass the proxy for normal model calls. FastAPI
uses `AI4IA_MODEL_GATEWAY_URL=https://<proxy>/openai` with a distinct opaque
proxy-ingress key that belongs to an APIM product with no APIs; it cannot invoke
the model API. The shared active model subscription is held only by SimpleL7Proxy.
Caller `Authorization`, APIM key, and internal app/user headers are stripped at
the proxy; APIM derives correlation from the proxy-owned request ID and uses
managed identity for Foundry. Voice Live fails startup if its URL/key are empty,
not distinct, or do not describe an HTTPS/WSS `/openai` gateway endpoint.

The APIM plane is the shared `apim-mcp-*` Basic v2 service (capacity 1), and it is
now the only APIM service in the environment; the prior Consumption APIM and all of
its children have been deleted.

### Speech Voice Live (second voice provider)

`speech_voice_live` is an additive, default-off second realtime provider selected
per connection by the browser's `provider` value (`azure_openai` remains the
server-authoritative default). It routes
`Browser -> FastAPI /api/voice/live -> separate shared Basic v2 APIM WebSocket API
-> Foundry`, never through SimpleL7Proxy, and never directly from the browser.

- `AI4IA_SPEECH_VOICE_LIVE_ENABLED` (`speechVoiceLiveEnabled` in Bicep, default
  `false`) is the master gate. It requires `AI4IA_REALTIME_ENABLED=true` and
  `speech_voice_live` present in `AI4IA_VOICE_PROVIDER_ALLOWLIST`. When `false`,
  the deployment emits no Speech URL/key into the API or web app and fresh
  deployments do not create the conditional Speech APIM children or
  Speech-specific Foundry User role assignment.
- `AI4IA_SPEECH_VOICE_LIVE_BASE_URL` must be an HTTPS/WSS APIM URL ending in
  `/speech/voice-live` (never a `services.ai.azure.com` or
  `cognitiveservices.azure.com` host), and `AI4IA_SPEECH_VOICE_LIVE_GATEWAY_API_KEY`
  must be a secret distinct from both `AI4IA_REALTIME_GATEWAY_API_KEY` and
  `AI4IA_MODEL_GATEWAY_API_KEY`. Startup fails closed if either is missing,
  malformed, or reused.
- The account, region, and API version remain fixed: the initial existing
  `eastus2` AIServices account at stable `2026-04-10` — the same account already
  used as a Foundry model backend, not a new resource. The managed-model selector
  accepts only the six catalog entries documented in the
  [region matrix](region-capability-matrix.md#speech-voice-live-managed-model-catalog).
  Native-audio models use `gpt-4o-transcribe`; GPT text response models use the
  Azure Speech chain and `azure-speech` transcription. Voice/locale/VAD/noise/echo
  capabilities come from the generated voice provider catalog
  (`infra/voice-providers.json`); only curated
  `azure-standard` built-in voices are offered and no custom endpoint, lexicon, or
  personal voice value is ever accepted.
- The shared active APIM's system-assigned managed identity authenticates to that
  account using a managed-identity audience set by the **deployment-only** Bicep
  parameter `speechVoiceLiveManagedIdentityAudience` (default `https://ai.azure.com`,
  matching the `azure-ai-voicelive` SDK default) — this is not an app runtime
  setting and the browser cannot influence it. Confirming that the selected account
  accepts this exact audience is a pending live-validation gate; do not change the
  default before that gate closes.
- APIM grants that identity **Cognitive Services User** and **Foundry User**
  (formerly Azure AI User) roles scoped only to the one selected AIServices
  account, on top of (not instead of) the roles it already holds on every Foundry
  backend.
- The Speech Voice Live APIM subscription (`ai4ia-api-speech-voice-live`) is scoped
  only to the `/speech/voice-live/realtime` WebSocket API; it cannot invoke
  `/openai/realtime`, the normal model API, the MCP plane, or the proxy ingress
  product, and none of those keys can invoke it.
- ARM Incremental mode does not remove Speech resources created by an earlier
  deployment when the flag is later set to `false`. A retained Speech API remains
  subscription-key protected, and no Speech subscription key is wired into the
  running API, but its APIM inventory and account-scoped role assignment remain
  dormant privilege until a separately approved targeted teardown removes them.
  No automatic teardown occurs.
- Usage records add a `provider` field (default `azure_openai`, back-compatible
  with existing records) and a nullable `deployment`; Speech Voice Live turns are
  metered against a truthful `managed_voice_live` target rather than inventing a
  deployment name. When Voice Live usage is not present on a response, the turn is
  persisted as `usageKnown=false`, `billable=false` — never a fabricated zero cost.

Browser preferences now use `ai4ia.voiceLive.prefs.v3`. A valid v2 value is
normalized and copied forward once; the new Speech managed-model preference
defaults to `gpt-realtime`. Azure OpenAI's deployment choice and Speech's managed
model/settings are separate fields. Inline changes are applied only when the next
Voice Live connection opens.

### Authenticated Voice Live operator canary

`scripts/voice-live-canary.py` is an operator-only diagnostic, not an API endpoint.
It requires the FastAPI app's exact secure `wss://.../api/voice/live` URL, an
allowed HTTPS Origin, provider, model, and an Entra API token read from a named
environment variable (default `AI4IA_VOICE_CANARY_TOKEN`). It sends the same
default `session.update` and bounded synthetic history frame shapes as the browser,
then succeeds only on `session.created` followed by `session.updated`:

```powershell
python scripts/voice-live-canary.py `
  --url wss://<api-host>/api/voice/live `
  --origin https://<web-origin> `
  --provider speech_voice_live `
  --model gpt-realtime `
  --token-env AI4IA_VOICE_CANARY_TOKEN
```

Populate the environment variable through the approved operator sign-in flow;
never put the token on the command line. `--region` is Azure OpenAI-only.
`--agent` and `--tools` are explicit governed opt-ins and default off. A direct
bare APIM handshake tests infrastructure only; it does not prove app auth, Origin,
entitlement, catalog resolution, normalization, or relay behavior.

The relay's `voice_live_completion` record carries `correlationId`, provider,
model/usage target, outcome, bounded protocol error and close metadata, source
event, and directional frame counts/event types. It deliberately excludes tokens,
keys, raw frames, audio, transcripts, prompts/history, and tool arguments/results.

APIM owns bounded immediate backend attempts. When all compatible regions are
throttled it returns `429`, `S7PREQUEUE: true`, and `retry-after-ms`.
SimpleL7Proxy uses `MaxAttempts=1` per dispatch and owns delayed requeue, avoiding
retry multiplication.

### Multi-application onboarding status

The infrastructure exposes the profile/priority/telemetry/async knobs, but shared
key ingress is not a sufficient application identity boundary. A new independent
application is therefore **not onboarded by adding a profile JSON row**.

Before enabling `proxyProfilesEnabled`, an operator must:

1. give each application a verifiable Entra workload identity (or equivalent
   cryptographically verified identity) at the proxy edge;
2. derive the profile lookup header at that trusted boundary rather than accepting
   a caller-supplied value;
3. export only the minimal app projection from canonical Cosmos into the
   secret-mounted snapshot; and
4. enforce per-app allowed paths/models and quotas at the proxy/APIM boundary.

The current upstream profile subsystem can add flat profile fields and influence
priority, but it does not securely authenticate `UserConfigUrl`, provide a Cosmos
projection job, or independently enforce the full allowed-path/model/quota
contract. `validate-feature-prereqs.py` intentionally rejects profile enablement
until that work exists.

## Validation

CI runs `scripts/validate-feature-prereqs.py` from `infra-validate.yml`. It
checks cross-parameter contradictions that Bicep syntax validation does not
catch: prod with dev auth, Entra without tenant/audience/client ID, Voice Live
tools without Voice Live, Speech Voice Live enabled without Voice Live or without
its allowlist/URL/key prerequisites, document compute without document
understanding, broken custom-domain/cert combinations, proxy scale/priority
errors, unsafe profile enablement, and personal ownership defaults. CI also
regenerates and tests the HTTP/SSE endpoint fragment and realtime routing policy
against `infra/models.json`, and the voice provider catalog
(`scripts/gen-voice-provider-catalog.py --check`) against
`infra/voice-providers.json`.
The proxy Container App also uses the upstream listener on port `8080` for
startup, liveness, and readiness. Optional async resources inherit the data-tier
public/private posture and emit diagnostics to the shared Log Analytics workspace.

### Basic v2 model/realtime gateway cutover

`AI4IA_MODEL_GATEWAY_URL` remains the SimpleL7Proxy `/openai` URL. Its API key is an opaque proxy-ingress key from an APIM product with no APIs, carried in `AI4IA_MODEL_GATEWAY_API_KEY_HEADER` (`S7P-KEY` in the deployed stack), so FastAPI cannot invoke the model API directly. SimpleL7Proxy strips that inbound header and alone injects its separate `Ocp-Apim-Subscription-Key` for the shared model API. Voice Live uses `AI4IA_REALTIME_BASE_URL=https://<shared-active-apim>/openai` and a third core credential, `AI4IA_REALTIME_GATEWAY_API_KEY`, scoped only to `/openai/realtime`. Voice Live startup fails when the URL/key are missing, equal to the proxy ingress key, or do not name an HTTPS/WSS `/openai` endpoint. Speech Voice Live adds an optional fourth, independently scoped pair — `AI4IA_SPEECH_VOICE_LIVE_BASE_URL` (`/speech/voice-live`) and `AI4IA_SPEECH_VOICE_LIVE_GATEWAY_API_KEY` — with fail-closed distinctness checks against the FastAPI-held proxy-ingress and realtime keys; the model-APIM key remains held only by SimpleL7Proxy.

The APIM plane is the shared `apim-mcp-*` Basic v2 service (capacity 1), and it is now the only APIM service in the environment; the previous Consumption APIM and all its children have been deleted.
