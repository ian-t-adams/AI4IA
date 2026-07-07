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
  OffMCP --> MCPGW[MCP APIM front door<br/>Basic v2 + subscription key]
  MCPGW --> MCPUp[Curated upstream MCP servers]
  API --> GW[APIM + SimpleL7Proxy<br/>model gateway]
  GW --> EUS2[Foundry East US 2]
  GW --> SWC[Foundry Sweden Central]
  GW --> WUS[Foundry West US]
```

Voice Live connects directly from the browser to the API because the Next.js HTTP
proxy cannot proxy WebSockets. The API relay still enforces auth, Origin checks,
entitlements, metering, deployment resolution, and optional governed tool calling.

## Request lifecycle

1. The browser authenticates through Entra/MSAL or local dev identity and calls the
   Next.js app.
2. The web app forwards API requests server-side with the current identity context
   and feature visibility from runtime environment variables.
3. The FastAPI service normalizes the user id, enforces admin/feature/tool gates,
   loads session state, and builds the governed model/tool plan.
4. Model calls route through APIM and SimpleL7Proxy to the selected Foundry
   deployment. Native Azure service planes such as Content Understanding, Azure
   Monitor, Key Vault, Blob Storage, Cosmos, and AI Search are called directly with
   managed identity or configured service auth.
5. Durable state is written to Cosmos and Blob Storage; derived memory/search/chunk
   stores are updated best-effort and can be rebuilt.

```mermaid
sequenceDiagram
  autonumber
  participant B as Browser
  participant W as Web (Next.js)
  participant A as API (FastAPI)
  participant G as APIM + Proxy
  participant F as Foundry
  participant C as Cosmos
  B->>W: POST /api/chat (Entra bearer)
  W->>A: forward same-origin (+ identity)
  A->>A: auth, normalize user id, entitlement + feature gates
  A->>C: load session + inject memory/document context
  A->>G: governed model call
  G->>F: managed-identity call to deployment
  F-->>A: streamed tokens
  A-->>B: stream response (via web proxy)
  A->>C: persist messages + usage ledger (best-effort)
```

## Core principles

1. **Gateway-first model calls.** Chat, agents, embeddings, image/video generation,
   speech, and realtime models route through APIM/SimpleL7Proxy. Non-chat Azure
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

## Components

| Layer | Tech | Notes |
|---|---|---|
| Web | Next.js + TypeScript | Chat, voice, library/media, admin, auth runtime config |
| API | FastAPI + Pydantic | Auth, chat, agents, tools, documents, usage, metrics |
| Agent runtime | In-process Python | Gateway-native tool loop and user-defined agents |
| Model gateway | APIM + SimpleL7Proxy | Routing, managed identity to Foundry, telemetry |
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
    Apim["apim · model gateway"]
    ApimMcp["apim-mcp · MCP gateway"]
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
  ProxyCA --> Apim
  ApiCA --> Apim --> AI
  ApiCA --> ApimMcp --> AI
  ApimMcp -. inventory .-> Apic
  ApiCA --> Data
  ApiCA --> Sec
  ApiCA --> Obs
  Compute --> Acr
```

## Identity and RBAC

AI4IA is keyless: every Azure data plane is reached through a user-assigned managed
identity and a scoped role assignment. No account keys, connection strings, or SQL
passwords are used at runtime.

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
  idapi -->|"AcrPull"| Acr
  apim["APIM (system MI)"] -->|"OpenAI User + Cognitive Services User"| Foundry["Foundry accounts"]
```

## MCP tool planes

Remote MCP tools reach the model through two independent planes that share one
governed per-turn executor:

- **BYO (bring-your-own).** Per-user servers a signed-in user registers. Called
  **directly** behind the SSRF guard (DNS-rebind re-validation at call time), with
  credentials in per-user Key Vault. Untrusted by default, so their tools are
  approval-gated.
- **Official (curated).** An admin-defined catalog (`infra/mcp-servers.json`)
  reached **through a dedicated MCP APIM front door** (`mcpgateway.bicep`, APIM
  Basic v2) gated on one app-global subscription key. Trusted ⇒ pre-approved. The
  model gateway stays a separate APIM so model traffic keeps its scale-to-zero
  economics.

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
  Official --> ApimMcp["MCP APIM front door<br/>(Basic v2 + subscription key)"]
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

- External ID/CIAM is not wired; current auth is Entra workforce/B2B.
- Folder-level document sharing and unauthenticated public links are not
  implemented; `public` documents are tenant-walled.
- The library UI does not yet expose custom analyzer authoring or first-class
  non-document modality uploads.
- Memory lacks a global user-facing toggle and recalled-memory indicator.
