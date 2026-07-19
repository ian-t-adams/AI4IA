# AI4IA Architecture

AI4IA is a governed, multi-model chat and agent app on Azure Container Apps. The
web frontend calls the FastAPI backend; the backend owns auth, session state,
tools, memory, document/library access, usage metering, and all model routing.

Editable visual: [`architecture-overview.excalidraw`](./architecture-overview.excalidraw).
Live, always-current view: the [status &amp; documentation portal](https://ian-t-adams.github.io/AI4IA/).

## High-level flow

```mermaid
flowchart TB
  User[Browser] -->|MSAL or dev auth| Web[Next.js web<br/>Container App]
  Web --> API[FastAPI API<br/>Container App]
  User -. Voice Live WS .-> API
  API --> Cosmos[(Cosmos DB<br/>sessions, usage, agents, docs)]
  API --> Pg[(Postgres + pgvector<br/>memory and doc chunks)]
  API --> Blob[(Blob Storage<br/>documents, media artifacts)]
  API --> Search[Azure AI Search<br/>optional doc retrieval]
  API --> Monitor[Azure Monitor / App Insights]
  API --> CU[Content Understanding]
  API --> Tools[Built-in tools + BYO MCP]
  API --> OffMCP[Official MCP tools]
  OffMCP --> APIM[Shared apim-mcp<br/>Basic v2 + scoped keys + MI]
  APIM --> MCPUp[Curated upstream MCP servers]
  API -->|proxy-ingress key| Proxy[SimpleL7Proxy<br/>HTTP/SSE queue + requeue]
  Proxy -->|model-API key| APIM
  API -. "azure_openai<br/>/openai/realtime key" .-> APIM
  API -. "speech_voice_live<br/>/speech/voice-live/realtime key" .-> APIM
  APIM --> EUS2[Foundry East US 2]
  APIM --> SWC[Foundry Sweden Central]
  APIM --> WUS[Foundry West US]
  APIM -.->|MI + curated model / 2026-04-10| SpeechAcct[Existing AIServices account<br/>voice-live/realtime]
```

Voice Live connects directly from the browser to the API because the Next.js HTTP
proxy cannot proxy WebSockets. The API relay still enforces auth, Origin checks,
entitlements, metering, deployment resolution, and optional governed tool calling.
Its upstream WebSocket goes to one of two separately scoped APIM WebSocket APIs on
the same shared active Basic v2 APIM, selected by the `provider` the browser sent:
`azure_openai` (catalog deployment routing) or `speech_voice_live` (curated managed
models, second provider, default OFF). SimpleL7Proxy is deliberately bypassed for
both because it supports HTTP/SSE, not WebSockets.

### Voice providers

AI4IA advertises two stable voice provider IDs — `azure_openai` and
`speech_voice_live` — behind one `/api/voice/live` relay and one inline chat
transcript:

```mermaid
flowchart LR
  Browser["Browser inline voice<br/>24 kHz PCM16"] -->|"WSS /api/voice/live<br/>provider ID only"| API["FastAPI relay<br/>auth, origin, gates, entitlements,<br/>agents/tools, persistence, metering"]
  API -->|"azure_openai<br/>scoped key"| AOAIAPI["APIM WebSocket API<br/>/openai/realtime"]
  API -->|"speech_voice_live<br/>distinct scoped key"| SpeechAPI["APIM WebSocket API<br/>/speech/voice-live/realtime"]
  AOAIAPI -->|"MI + deployment routing"| AOAI["Foundry Azure OpenAI Realtime"]
  SpeechAPI -->|"MI + approved model / 2026-04-10"| Speech["Existing AIServices account<br/>eastus2 · /voice-live/realtime"]
```

- `azure_openai` is the default-safe provider: it stays enabled and default unless
  an operator deliberately reconfigures `AI4IA_VOICE_DEFAULT_PROVIDER`. It resolves
  a realtime model/region from `infra/models.json` exactly as before.
- `speech_voice_live` is an additive, default-off second provider. Its stable
  `2026-04-10` contract on the existing `eastus2` AIServices account allows six
  catalog models: native-audio `gpt-realtime` / `gpt-realtime-mini` with
  `gpt-4o-transcribe`, and `gpt-4.1`, `gpt-4.1-mini`, `gpt-5-mini`, and `gpt-5.1`
  through the Azure Speech chain with `azure-speech` transcription. Only curated
  `azure-standard` built-in voices/capabilities are accepted; custom endpoints,
  lexicons, and personal voices remain blocked.
- Both providers share the same governed relay path (auth, Origin, feature and
  entitlement checks, agents/tools, transcript persistence, provider-aware usage
  metering, and cleanup) and the same inline transcript/session. A provider switch
  applies only to the **next** connection; it never triggers a silent reconnect of
  an active session. Azure OpenAI deployment preferences and Speech managed-model
  preferences are stored independently, so switching providers does not overwrite
  either provider's settings.
- Each provider holds a distinct APIM subscription key scoped to its own
  WebSocket API, so neither key can invoke the other provider's API, the normal
  model API, the MCP plane, or the proxy ingress product.

## Request lifecycle

1. The browser authenticates through Entra/MSAL or local dev identity and calls the
   Next.js app.
2. The web app forwards API requests server-side with the current identity context
   and feature visibility from runtime environment variables.
3. The FastAPI service normalizes the user id, enforces admin/feature/tool gates,
   loads session state, and builds the governed model/tool plan.
4. Compatible model calls route through SimpleL7Proxy, then APIM, to a
   catalog-selected Foundry deployment. APIM performs bounded immediate regional
   failover; a full-capacity `429` carries the `S7PREQUEUE`/`retry-after-ms`
   contract back to the proxy for delayed requeue. Native Azure service planes
   such as Content Understanding, Azure
   Monitor, Key Vault, Blob Storage, Cosmos, and AI Search are called directly with
   managed identity or configured service auth.
5. Durable state is written to Cosmos and Blob Storage; derived memory/search/chunk
   stores are updated best-effort and can be rebuilt.

## Conversation policy and inspector

The API owns the active conversation policy. Sessions add optional, backward-compatible
`agentName`, `toolOverrides`, and `libraryDocumentIds` fields; existing Cosmos records
need no migration. A missing/null `libraryDocumentIds` preserves legacy access to all
accessible ready documents, an explicit empty list disables library context, and a
non-empty list is an exact allowlist. Processing/failed documents may stay selected for
status visibility, but only ready selected documents enter model context or document
tools. Instruction precedence is:
Selection validation and inspection resolve both owned documents and email-shared
documents through the same access predicate. Converting a legacy all-accessible
session to an explicit scope preserves every currently accessible owned/shared id;
revoked or stale shared ids never regain retrieval/tool access. Instruction precedence is:

1. the selected governed agent persona;
2. otherwise the session `systemPrompt`;
3. otherwise the provider default.

Typed chat and both Voice Live providers resolve this same policy. The browser never
supplies authoritative voice instructions; the WebSocket binds to an owned session and
the relay replaces or removes client instructions before forwarding `session.update`.
Tool overrides can only add server-approved tools or remove inherited tools, and
execution still re-checks registry, scope, approval, target-host, MCP ownership, and
SSRF rules.

Tool metadata declares typed-chat and Voice Live availability. The realtime relay
advertises only validated registry-backed voice-capable tools; synthetic document,
image/video, and MCP tools remain typed-only until a safe authenticated realtime
handler exists.

`GET /api/sessions/{id}/inspector` provides a display-safe, ownership-scoped snapshot
for the right Conversation Inspector. Focused APIs continue to own mutations and
memory, library, tool-catalog, and usage detail.
Inspector sources load independently and all mutation results are session-generation
guarded. The admin dashboard similarly aborts and discards superseded window/identity
loads.
Session policy fields use atomic repository patches, while library-document list
changes use bounded ETag/CAS retry-and-merge. Concurrent model, prompt, tool, and
document-selection writes therefore preserve disjoint changes in both Cosmos and
the in-memory parity repository.
All chat, workflow, command, and summarization writers use those field-scoped
patch/touch APIs. Unversioned full-session replacement is a guarded failure in both
repositories, preventing stale conversational workers from overwriting workspace
policy or document selections.
Rolling summary state carries a backward-compatible monotonic `summaryVersion`.
Clear/reset increments that version while atomically clearing the summary and cursor;
manual and automatic summarizers commit only when their observed version still
matches, then increment it. A clear or newer summarizer therefore makes stale output
a benign discard rather than allowing pre-clear context to reappear.

The admin operations plane runs fixed server-owned KQL through managed identity
against the existing Log Analytics workspace. It accepts only a bounded time window,
never user KQL, and returns per-panel source/freshness/partial/stale/unavailable state.

```mermaid
sequenceDiagram
  autonumber
  participant B as Browser
  participant W as Web (Next.js)
  participant A as API (FastAPI)
  participant P as SimpleL7Proxy
  participant G as APIM
  participant F as Foundry
  participant C as Cosmos
  B->>W: POST /api/chat (Entra bearer)
  W->>A: forward same-origin (+ identity)
  A->>A: auth, normalize user id, entitlement + feature gates
  A->>C: load session + inject memory/document context
  A->>P: governed HTTP/SSE model call
  P->>G: authenticated model hop
  G->>F: managed-identity call to deployment
  F-->>A: streamed tokens
  A-->>B: stream response (via web proxy)
  A->>C: persist messages + usage ledger (best-effort)
```

## Core principles

1. **Gateway-first model calls.** Chat, agents, embeddings, image/video generation,
   and REST speech route SimpleL7Proxy -> APIM. Realtime/Voice Live (both
   `azure_openai` and `speech_voice_live`) stays on the governed FastAPI relay ->
   APIM path because the proxy is not a WebSocket server. Non-chat Azure
   service planes such as Content Understanding and Azure Monitor use their native
   endpoints with managed identity or configured service auth.
2. **Catalog-driven deployments.** `infra/models.json` is the deployment source of
   truth and generates the packaged API model catalog. Capability models stay out
   of chat pickers unless their category is explicitly allowed.
3. **Feature-gated advanced surfaces.** Voice Live, document understanding,
   document compute, inline attachment compute, custom (BYO) MCP tools, the
   official APIM-fronted MCP plane, Web IQ, search, image/video generation, and
   memory all have explicit config gates and fail-closed prerequisites.
4. **Cosmos is canonical; derived stores are rebuildable.** Sessions, messages,
   usage, user agents/workflows, MCP server records, and document manifests live in
   Cosmos. Memory vectors, document chunks, search indexes, and parsed artifacts
   can be regenerated from canonical records and blobs.
5. **Per-user isolation.** User ids are normalized at the API boundary. Cosmos
   partitions, blob prefixes, vector filters, memory records, and MCP secrets are
   scoped to the authenticated user; document sharing widens reads through a
   single access gate.
6. **Telemetry without blocking the hot path.** Usage writes, custom events,
   resource metrics, and optional App Insights export are best-effort and never
   break a chat turn.

Voice Live completion telemetry is metadata-only: correlation id, provider, model
or usage target, outcome, bounded protocol-error/close metadata, source event, and
directional text/binary frame counts and event types. It excludes credentials,
raw frames, audio, transcripts, prompts/history, and tool arguments/results.

## Gateway execution boundaries

- DNS/custom domain terminates on the public SimpleL7Proxy Container App. APIM is
  the proxy's backend and never routes back to the proxy.
- FastAPI holds two distinct keys, never the proxy's model-API key: a proxy-ingress
  key (`AI4IA_MODEL_GATEWAY_API_KEY`) that authenticates normal calls to the proxy,
  scoped to a product with no APIs so it cannot invoke any model or realtime API;
  and a realtime-only key (`AI4IA_REALTIME_GATEWAY_API_KEY`) used only for the
  direct FastAPI -> APIM realtime hop. The proxy strips the ingress key at the edge
  and injects its own, third key — held only by the proxy, for the proxy -> APIM
  model hop — before forwarding; FastAPI never receives that key.
  `Settings.validate_runtime()` fails startup if the realtime key and the
  proxy-ingress key are ever the same value.
- `speech_voice_live` adds a fourth, independently scoped APIM subscription held
  only by FastAPI, bound to the `/speech/voice-live/realtime` WebSocket API. That
  key cannot invoke `/openai/realtime`, the normal model API, the MCP plane, or the
  proxy ingress product, and none of the other three keys can invoke it either.
- APIM performs bounded immediate attempts across compatible regional
  deployments. SimpleL7Proxy performs delayed requeue and owns queue TTL and
  per-replica circuit breaking. The synchronous queue is not durable or global.
- Event Hub carries optional metadata telemetry only. Dedicated Blob and Service
  Bus resources are provisioned only for explicit durable async mode.
- Cosmos remains canonical. The proxy has no Cosmos RBAC. A minimal profile
  snapshot can be mounted as a secret file, but enablement is blocked until the
  edge can prove application identity.

## Components

| Layer | Tech | Notes |
|---|---|---|
| Web | Next.js + TypeScript | Chat, voice, library/media, admin, auth runtime config |
| API | FastAPI + Pydantic | Auth, chat, agents, tools, documents, usage, metrics |
| Agent runtime | In-process Python | Gateway-native tool loop and user-defined agents |
| Model gateway | SimpleL7Proxy + APIM | HTTP/SSE priority queue and delayed requeue; catalog routing and managed identity to Foundry |
| App data | Cosmos DB | Sessions, messages, usage, agents, workflows, documents |
| Memory/chunks | Postgres + pgvector / mem0 | Per-user semantic recall and document chunks |
| Search | Azure AI Search | Optional hybrid/semantic document retrieval backend |
| Storage | Blob Storage | Raw documents, parsed artifacts, generated media |
| AI services | Foundry + Content Understanding | Models, realtime, speech, image/video, CU ingest |
| Observability | Log Analytics + App Insights + Monitor | Logs, traces, metrics, admin resource panels |

## Deployment topology

The live footprint in `rg-ai4ia-slurmfactory` (East US 2 primary), grouped by role.
A live, always-current view is published on the
[status portal](https://ian-t-adams.github.io/AI4IA/status.html).

```mermaid
flowchart LR
  subgraph Edge["Edge / ingress"]
    WebCA["ca-web · Next.js"]
    ProxyCA["ca-proxy · SimpleL7Proxy"]
  end
  subgraph Gateways["API Management"]
    Apim["apim · Consumption rollback"]
    ApimMcp["apim-mcp · shared model/realtime/MCP gateway"]
    Apic["API Center · tool catalog"]
  end
  subgraph Compute["Container Apps env + registry"]
    ApiCA["ca-api · FastAPI"]
    Acr["ACR"]
  end
  subgraph AI["Azure AI Foundry (x3 regions)"]
    F1["East US 2"]
    F2["Sweden Central"]
    F3["West US"]
  end
  subgraph Data["Data + memory"]
    Cos["Cosmos DB"]
    Pg["Postgres + pgvector"]
    Srch["AI Search"]
    St["Storage x2"]
  end
  subgraph Sec["Security / config"]
    Kv["Key Vault"]
    Ac["App Configuration"]
    Ids["Managed identities x3"]
  end
  subgraph Obs["Observability"]
    Ai["App Insights"]
    La["Log Analytics"]
    Amw["Monitor workspace"]
  end
  WebCA --> ApiCA
  ApiCA --> ProxyCA --> ApimMcp --> AI
  ApiCA -. "azure_openai + speech_voice_live<br/>realtime WS (distinct keys)" .-> ApimMcp
  ApiCA --> ApimMcp --> AI
  ApimMcp -. inventory .-> Apic
  ApimMcp -. "inactive rollback plane" .-> Apim
  ApiCA --> Data
  ApiCA --> Sec
  ApiCA --> Obs
  Compute --> Acr
```

## Identity and RBAC

Azure service data planes use managed identities and scoped role assignments. The
current proxy/APIM transition uses three independently scoped APIM subscription
keys: one held only by the proxy for proxy -> normal-model APIM, one proxy-ingress
key held only by FastAPI for FastAPI -> proxy, and one realtime-only key held only
by FastAPI for the direct FastAPI -> APIM realtime hop. All three remain Container
App secrets; the proxy strips the ingress key before forwarding and injects its own
model key. The migration target is Entra workload authentication on every hop.
`speech_voice_live` adds a fourth, distinct APIM subscription key (`ai4ia-api-speech-voice-live`)
held only by FastAPI and scoped only to the Speech Voice Live WebSocket API.

The shared active APIM's system-assigned managed identity gets the additional
**Foundry User** role (formerly Azure AI User) on the single existing AIServices
account Speech Voice Live is approved to reach, on top of the **Cognitive Services
User** role every Foundry backend already grants it. Both roles are scoped only to
that one account, never broadened to every regional Foundry backend. APIM
authenticates to it using a managed-identity audience (`speechVoiceLiveManagedIdentityAudience`,
default `https://ai.azure.com`) that is a deployment-only Bicep parameter, not a
runtime setting the app or browser can influence; confirming that this specific
account accepts that audience remains a pending live-validation gate tracked in
the operator's local (gitignored) approval plan, not this repository's published docs.

```mermaid
flowchart LR
  idapi["id-api"] -->|"Cosmos Data Contributor"| Cos["Cosmos DB"]
  idapi -->|"Storage Blob Data Contributor"| St["Storage x2"]
  idapi -->|"Search Index + Service Contributor"| Srch["AI Search"]
  idapi -->|"Key Vault Secrets User / Officer"| Kv["Key Vault"]
  idapi -->|"App Config Data Reader"| Ac["App Configuration"]
  idapi -->|"Monitoring Reader (sub scope)"| Mon["Azure Monitor"]
  idapi -->|"Entra token"| Pg["Postgres"]
  idweb["id-web"] -->|"AcrPull"| Acr["ACR"]
  idproxy["id-proxy"] -->|"AcrPull"| Acr
  idproxy -->|"App Config Data Reader"| Ac
  idproxy -->|"Event Hubs Data Sender (optional)"| EH["Event Hubs"]
  idproxy -->|"Blob Data Contributor + Service Bus sender/receiver (optional async)"| Async["Async stores"]
  idapi -->|"AcrPull"| Acr
  apim["APIM (system MI)"] -->|"OpenAI User + Cognitive Services User"| Foundry["Foundry accounts"]
  apim -->|"Cognitive Services User + Foundry User"| SpeechAcct["Selected AIServices account<br/>(Speech Voice Live)"]
```

## MCP tool planes

Remote MCP tools reach the model through two independent planes that share one
governed per-turn executor:

- **BYO (bring-your-own).** Per-user servers a signed-in user registers. Called
  **directly** behind the SSRF guard (DNS-rebind re-validation at call time), with
  credentials in per-user Key Vault. Untrusted by default, so their tools are
  approval-gated.
- **Official (curated).** An admin-defined catalog (`infra/mcp-servers.json`)
  reached **through the shared `apim-mcp-*` APIM** (`apimcore.bicep` owns the
  Basic v2 service; `mcpgateway.bicep` owns its MCP children)
  with a product-scoped app-global subscription key. Trusted ⇒ pre-approved. The
  MCP product associates only MCP APIs, so its key cannot call the model/realtime
  APIs. The legacy Consumption model gateway remains a temporary rollback plane.

The BYO plane is **default-OFF**. The official plane's bicep params default off too, but in this
repo it is **activated**: `enableOfficialMcp=true` and the catalog registers the Foundry toolbox
(`ai4ia-toolbox`). Each turn builds the official plane first and BYO second, then merges them: on a
tool name collision the **official tool wins**, auto-approvals are unioned, and a single per-turn
budget caps total MCP calls across both planes.

```mermaid
flowchart TB
  Turn["Chat turn executor<br/>(per-turn budget + approvals)"]
  Turn --> Official["Official plane (trusted, pre-approved)"]
  Turn --> Byo["BYO plane (untrusted, approval-gated)"]
  Official --> ApimMcp["Shared apim-mcp APIM<br/>(Basic v2 + MCP-only product key)"]
  ApimMcp --> Curated["Curated upstream MCP servers<br/>(infra/mcp-servers.json)"]
  Byo -. "SSRF guard (DNS-rebind re-check)" .-> UserSrv["Per-user MCP servers"]
  UserSrv --> Kv["Per-user secrets in Key Vault"]
```

## Regions

See [region-capability-matrix.md](./region-capability-matrix.md). The default
strategy uses:

- **East US 2** for the primary US model set, realtime, image/video, router, and
  evaluations.
- **Sweden Central** for EU-resident model parity where available.
- **West US** for targeted models such as MAI Image and deep research.

## Current gaps

- Multi-application profiles remain default-off and the deployment validator
  refuses to enable them while ingress uses a shared key. A verified
  identity-aware application header (target: Entra workload auth) is required
  before the mounted server-owned profile projection may become authoritative.
- SimpleL7Proxy queue priority/fairness state is in-memory and per replica. Keeping
  a warm replica removes cold-start risk but does not create durable, global
  ordering across replicas.
- External ID/CIAM is not wired; current auth is Entra workforce/B2B.
- Folder-level document sharing and unauthenticated public links are not
  implemented; `public` documents are tenant-walled.
- The library UI does not yet expose custom analyzer authoring or first-class
  non-document modality uploads.
- Memory lacks a global user-facing toggle and recalled-memory indicator.
- `speech_voice_live` is implemented but disabled by default
  (`speechVoiceLiveEnabled=false`) pending a separately approved live-validation
  pass confirming the selected AIServices account's Cognitive Services User +
  Foundry User RBAC and managed-identity audience, an APIM policy compiler run,
  and a zero-delete production what-if. It must not be enabled before those
  approvals close.

### APIM replacement cutover

The active APIM is a deterministic Basic v2 service with system-assigned identity. It carries both catalog-routed HTTP/SSE and the WSS realtime onHandshake API from one gateway base with separately scoped keys, including the additive `speech_voice_live` WebSocket API and its own distinct subscription. The prior Consumption APIM remains fully configured but receives no active traffic during a temporary stabilization window. This overlap is migration state, not permanent architecture; removing Consumption is a later destructive change with separate approval.
