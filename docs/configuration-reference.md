# Configuration Reference

AI4IA has too many knobs to leave them scattered across Bicep, azd, Container
App env, and README prose. This page is the operator map: set the azd value,
watch the Bicep parameter, and know which runtime setting appears in the app.

## Required deployment ownership values

| Purpose | azd / CI variable | Bicep parameter | Notes |
| --- | --- | --- | --- |
| Accountable owner tag | `AI4IA_OWNER` | `owner` | Do not leave this as a personal fork default. It lands on resource tags. |
| Cost center tag | `AI4IA_COST_CENTER` | `costCenter` | Defaults to `genai-demo`; override for real chargeback. |
| APIM publisher email | `AI4IA_APIM_PUBLISHER_EMAIL` | `apimPublisherEmail` | Required by API Management. Use an operator-owned mailbox, not a person. |
| Budget alert recipients | Bicep override only today | `budgetAlertEmails` | Empty means budget tracking without email notifications. |

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

| Feature | azd / CI variable | Bicep parameter | Runtime setting emitted | Required companion config |
| --- | --- | --- | --- | --- |
| Voice Live | `AI4IA_REALTIME_ENABLED` | `voiceLiveEnabled` | `AI4IA_REALTIME_ENABLED`, `VOICE_LIVE_ENABLED`, `API_PUBLIC_URL` | `realtimeAllowedOrigins` outside local/dev. |
| Voice Live tools | `AI4IA_REALTIME_TOOLS_ENABLED` | `voiceLiveToolsEnabled` | `AI4IA_REALTIME_TOOLS_ENABLED`, `VOICE_LIVE_TOOLS_ENABLED` | Requires Voice Live. |
| Document library / Content Understanding | `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED` | `documentUnderstandingEnabled` | `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED`, `DOCUMENT_LIBRARY_ENABLED` | Cosmos + blob storage; CU endpoint defaults to the primary Foundry endpoint unless overridden. |
| Library compute / export | `AI4IA_DOCUMENT_COMPUTE_ENABLED` | `documentComputeEnabled` | `AI4IA_DOCUMENT_COMPUTE_ENABLED` | Requires document understanding. Code Interpreter endpoint/model default to primary Foundry + `gpt-4.1-mini-*` unless overridden. |
| Inline attachment compute | `AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED` | `inlineDocumentComputeEnabled` | `AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED` | Uses the same Code Interpreter endpoint/model as library compute. |
| Azure AI Search | `AI4IA_SEARCH_LOCATION` | `searchEnabled`, `searchLocation` | `AI4IA_SEARCH_ENDPOINT` when provisioned | Region must have Search SKU capacity. |
| Image generation | checked-in parameter | `imageGenerationEnabled` | `AI4IA_IMAGE_BLOB_ACCOUNT_URL` when provisioned | Image-capable model deployment and generated-media storage. |
| Video generation | checked-in parameter | `videoGenerationEnabled` | `AI4IA_VIDEO_BLOB_ACCOUNT_URL` when provisioned | Video-capable model deployment and generated-media storage. |
| Custom MCP tools | checked-in parameter | `customToolsEnabled` | `AI4IA_CUSTOM_TOOLS_ENABLED`, `CUSTOM_TOOLS_ENABLED` | Cosmos + Key Vault; Entra auth outside local/dev. |
| Official MCP plane (APIM-fronted) | checked-in parameter | `enableOfficialMcp` | `AI4IA_OFFICIAL_MCP_ENABLED`, `AI4IA_OFFICIAL_MCP_GATEWAY_URL`, `AI4IA_OFFICIAL_MCP_SUBSCRIPTION_KEY` (secret) | Dedicated MCP APIM (Basic v2) + at least one entry in `infra/mcp-servers.json`. Gateway URL + key are wired module→module; enabled-without-both fails closed at startup. |
| Foundry toolbox (bridge) | checked-in parameter | `enableFoundryToolbox` (+ `enableOfficialMcp`) | consumed via the official MCP plane; `AZURE_FOUNDRY_PROJECT_ENDPOINT` output feeds `scripts/provision-foundry-*.py` | Toolbox is consumed as one `infra/mcp-servers.json` entry (no app runtime setting). Flag grants the MCP APIM MI the project "Foundry User" role. Provision the toolbox first; preview. See [`foundry-toolbox.md`](foundry-toolbox.md). |
| Private tool catalog (API Center) | `AI4IA_API_CENTER_LOCATION` | `enablePrivateToolCatalog`, `apiCenterLocation` | `AZURE_API_CENTER_NAME` output feeds `scripts/provision-private-tool-catalog.py` | Provisions an Azure API Center to inventory the APIM-fronted MCP servers (admin/IaC only, no app runtime setting). `apiCenterLocation` defaults to `eastus` because API Center is unavailable in some regions (notably `eastus2`). Asset registration is a documented script step; preview. See [`foundry-toolbox.md`](foundry-toolbox.md). |
| Web IQ search tools | `AI4IA_WEBIQ_API_KEY` (secret) | `webSearchEnabled`, `webIqApiKey` | `AI4IA_WEB_SEARCH_ENABLED`, `AI4IA_WEBIQ_API_KEY` (secret) | API key **or** an Entra managed identity entitled to Web IQ. In CI the key is a `production` environment secret; if unset, the api falls back to the managed identity and calls 401 unless it is entitled. Diagnose with the admin **Web search health** panel. |
| Memory / semantic recall | `AI4IA_POSTGRES_LOCATION` | `postgresLocation` | `AI4IA_MEMORY_STORE=mem0` when Postgres is provisioned | Region must allow PostgreSQL Flexible Server for the subscription. |
| Proxy application profiles | `AI4IA_PROXY_PROFILES_ENABLED`, `AI4IA_PROXY_PROFILE_PROJECTION_JSON` (secret) | `proxyProfilesEnabled`, `proxyProfileProjectionJson` | `UseProfiles`, `UserConfigRequired`, secret-mounted `file:/mnt/ai4ia-profiles/profiles.json` | **Blocked by validation while shared-key ingress is used.** Requires a verified identity-aware application header; no public/unauthenticated profile URL is permitted. |
| Proxy priority reservations | `AI4IA_PROXY_PRIORITIES_ENABLED`, `AI4IA_PROXY_PRIORITY_WORKERS` | `proxyPrioritiesEnabled`, `proxyPriorityWorkers` | `PriorityWorker`, `PriorityKeys`, `PriorityValues` | Worker map must use `priority:count` pairs. Off means no reserved workers. Queue fairness is per replica. |
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
`infra/models.json`. The generated policy fragment rewrites region-specific
deployment names and rejects unknown deployments rather than falling back.

Voice Live uses `AI4IA_REALTIME_BASE_URL=https://<apim>/openai` from the FastAPI
relay. Its APIM subscription is scoped only to `/openai/realtime`, so it cannot
be used to bypass the proxy for normal model calls. The normal-model APIM key is
held only by the proxy. Caller `Authorization`, APIM key, and internal app/user
headers are stripped at the proxy; APIM derives correlation from the proxy-owned
request ID and uses managed identity for Foundry.

APIM owns bounded immediate backend attempts. When all compatible regions are
throttled it returns `429`, `S7PREQUEUE: true`, and `retry-after-ms`.
SimpleL7Proxy uses `MaxAttempts=1` per dispatch and owns delayed requeue, avoiding
retry multiplication.

## Validation

CI runs `scripts/validate-feature-prereqs.py` from `infra-validate.yml`. It
checks cross-parameter contradictions that Bicep syntax validation does not
catch: prod with dev auth, Entra without tenant/audience/client ID, Voice Live
tools without Voice Live, document compute without document understanding,
broken custom-domain/cert combinations, proxy scale/priority errors, unsafe
profile enablement, and personal ownership defaults. CI also regenerates and
tests `infra/policies/simplel7proxy-endpoints.xml` against `infra/models.json`.
