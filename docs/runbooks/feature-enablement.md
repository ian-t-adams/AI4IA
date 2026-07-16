# Runbook: Feature Enablement

Most advanced AI4IA surfaces are implemented but gated. Defaults in code/Bicep are
safe; the live posture is controlled by `infra/main.parameters.json`, azd env
values, and Container App env. Startup validation in
`app/api/src/ai4ia_api/config.py` fails closed for half-wired deployed features.
Use the consolidated parameter/env map in
[`../configuration-reference.md`](../configuration-reference.md) before changing
feature posture.

## Flag inventory

| Feature | API flag / setting | Web flag | IaC parameter | Deployed prerequisites |
|---|---|---|---|---|
| Voice Live | `AI4IA_REALTIME_ENABLED` | `VOICE_LIVE_ENABLED` + `API_PUBLIC_URL` | `voiceLiveEnabled` | Browser Origin allowlist outside local |
| Voice Live tools | `AI4IA_REALTIME_TOOLS_ENABLED` | advertised by web env | `voiceLiveToolsEnabled` | Voice Live enabled |
| Document library + multimodal understanding | `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED` | `DOCUMENT_LIBRARY_ENABLED` | `documentUnderstandingEnabled` | Cosmos session store, blob account URL, CU endpoint outside local |
| Library compute / export | `AI4IA_DOCUMENT_COMPUTE_ENABLED` | none | `documentComputeEnabled` | Document understanding, Responses API base URL + model outside local |
| Inline attachment Code Interpreter | `AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED` | none | `inlineDocumentComputeEnabled` | Responses API base URL + model outside local |
| Azure AI Search chunk store | `AI4IA_SEARCH_ENDPOINT` set | none | `searchEnabled` + `searchLocation` | Search service + API identity RBAC |
| Memory / semantic recall | `AI4IA_MEMORY_STORE` | save/forget controls only | `postgresLocation` derives `mem0` when non-empty | Postgres host/user for `pgvector` or `mem0` |
| Image generation | `AI4IA_IMAGE_BLOB_ACCOUNT_URL` when provisioned | Settings / imagery UI | `imageGenerationEnabled` | Image-capable model deployment and media blob storage |
| Video generation | `AI4IA_VIDEO_BLOB_ACCOUNT_URL` when provisioned | inline attachment rendering | `videoGenerationEnabled` | Sora-capable deployment and media blob storage |
| Custom MCP tools | `AI4IA_CUSTOM_TOOLS_ENABLED` | `CUSTOM_TOOLS_ENABLED` | `customToolsEnabled` | Cosmos, Key Vault URI, Entra auth outside local |
| Official MCP plane | `AI4IA_OFFICIAL_MCP_ENABLED` | none | `enableOfficialMcp` | MCP-only product/subscription on the shared active Basic v2 APIM + ≥1 server in `infra/mcp-servers.json`; gateway URL + key auto-wired |
| Foundry toolbox (bridge) | consumed via the official MCP plane (no dedicated flag) | none | `enableFoundryToolbox` (+ `enableOfficialMcp`) | Provisioned toolbox in the default Foundry project + a `foundry-toolbox` entry in `infra/mcp-servers.json`; grants APIM MI the project "Foundry User" role. See [`../foundry-toolbox.md`](../foundry-toolbox.md) |
| Private tool catalog (API Center) | admin/IaC only (no app-runtime env) | none | `enablePrivateToolCatalog` | Provisions an Azure API Center to inventory the APIM-fronted MCP servers; asset registration is a documented script step (`scripts/provision-private-tool-catalog.py`). See [`../foundry-toolbox.md`](../foundry-toolbox.md) |
| Web IQ search tools | `AI4IA_WEB_SEARCH_ENABLED` | none | `webSearchEnabled` | Web IQ API key or Entra managed identity outside local |
| Admin resource panels | `AI4IA_RESOURCE_METRICS_ENABLED` + resource ids | admin dashboard | resource-id env from modules | Monitoring Reader and ARM resource ids |
| Proxy application profiles | proxy runtime only | none | `proxyProfilesEnabled` | Secret-mounted minimal projection **and verified identity-aware app header**; validator blocks enablement with shared-key ingress |
| Proxy priority reservations | proxy runtime only | none | `proxyPrioritiesEnabled`, `proxyPriorityWorkers` | Valid `priority:count` reservations; per-replica fairness only |
| Proxy metadata telemetry | proxy runtime only | none | `proxyEventHubTelemetryEnabled` | Existing Event Hub sender RBAC; no prompt/response/header logging |
| Proxy durable async | proxy runtime only | none | `proxyAsyncEnabled` | Dedicated AVM Blob + Service Bus resources and proxy MI RBAC |

The checked-in live parameters currently turn on image/video generation,
document understanding, document compute, inline-attachment code interpreter, AI
Search, Voice Live + tools, custom tools, Web IQ search, Postgres-backed memory,
and — as of the Foundry activation — the **official MCP plane, the Foundry toolbox
bridge, and the private tool catalog** (`enableOfficialMcp` / `enableFoundryToolbox`
/ `enablePrivateToolCatalog` are all `true`). The new proxy profile, priority,
Event Hub, and durable-async controls remain `false`; their Bicep defaults are
also off.

## Enablement notes

### Voice Live

Set:

```text
voiceLiveEnabled=true
realtimeAllowedOrigins=https://<web-origin>
voiceLiveToolsEnabled=true        # optional
```

The browser connects directly to the API ingress for `/api/voice/live`; the API
relay validates auth and Origin, resolves the realtime deployment from the model
catalog, and opens the upstream socket through the model gateway. An empty Origin
allowlist is allowed only in local.

The upstream socket is `FastAPI -> APIM -> Foundry`. It does **not** traverse
SimpleL7Proxy because that worker does not support WebSockets. The relay's APIM
subscription is scoped to the realtime API only. The active APIM must be the
WebSocket-capable Basic v2 replacement; Consumption does not support WebSocket
APIs. Startup fails closed when the replacement `/openai` URL or distinct realtime
subscription key is absent or malformed.

Basic v2 capacity 1 has an approximately $150/month base cost before calls and is
a single-region, single-unit production gateway. The prior Consumption service is
retained unchanged only as an inactive HTTP/SSE rollback plane during stabilization;
its deletion is a later destructive operation requiring separate approval.

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
- `pgvector` — custom Postgres + pgvector store.
- `mem0` — mem0 OSS over Postgres + pgvector with gateway-backed extraction and
  embeddings.

In IaC, a non-empty `postgresLocation` provisions Postgres and derives
`memoryStore='mem0'`. The live default uses `centralus` because the subscription
is offer-restricted for Postgres Flexible Server in several app regions.

Gap: automatic recall is available, plus document save/forget, but the chat UI
does not yet expose a global memory toggle or recalled-memory indicator.

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

The API exposes web/news/video/image/browse tools to tool-enabled turns, sanitizes
and nonce-fences returned content, and caps per-turn search fan-out. Outside local
it fails closed unless an API key or Entra managed identity is configured.

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

## Operational reminders

- Enabling a feature is a deploy and cost action; validate in a parallel resource
  group before changing a live environment.
- `infra/main.parameters.json` documents this repo's checked-in live posture, not
  the universal defaults.
- If a feature is disabled, its route/service either refuses with 404/disabled
  semantics or is never constructed.
