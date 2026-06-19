# Runbook: Feature Enablement (flag-off inventory)

> Every feature below ships **default-OFF** and is inert until explicitly enabled. This runbook is
> the single source of truth for what is gated, the exact flags/resources to turn each on, and the
> prerequisites that are enforced at startup (the API **fails closed** on a half-wired enable).
>
> Source of truth for defaults: `app/api/src/ai4ia_api/config.py`, `app/web/.env.example`,
> `infra/main.bicep`, `infra/modules/{api,web}.bicep`, `infra/models.json`.

## At a glance

| Feature | API flag (default) | Web flag (default) | IaC param (default) | External resources needed | Lift |
|---|---|---|---|---|---|
| Voice Live (Phase 10, agent-aware) | `AI4IA_REALTIME_ENABLED=false` | `VOICE_LIVE_ENABLED=false` + `API_PUBLIC_URL` | `voiceLiveEnabled=false` | none — `gpt-realtime` already deployed | **Low** |
| Realtime tools in voice | `AI4IA_REALTIME_TOOLS_ENABLED=false` | — | `voiceLiveToolsEnabled=false` (inert unless realtime on) | none | **Low** |
| Document library (11A/11B, + 11D–11F) | `AI4IA_DOCUMENT_UNDERSTANDING_ENABLED=false` | `DOCUMENT_LIBRARY_ENABLED=false` | `documentUnderstandingEnabled=false` | Cosmos store + blob account + Content Understanding endpoint | **High** |
| Document compute (11C) | `AI4IA_DOCUMENT_COMPUTE_ENABLED=false` | — | `documentComputeEnabled=false` | above **+** Azure OpenAI Responses/code-interpreter resource | **High** |
| AI Search chunk store (Phase 11) | `AI4IA_SEARCH_ENDPOINT` (set ⇒ used) | — | `searchEnabled=false` + `searchLocation` | Azure AI Search service | **Med** |
| Memory / semantic recall (Phase 5) | `AI4IA_MEMORY_STORE=disabled` | — (per-doc save/forget UI) | `postgresLocation` (empty ⇒ `memoryStore='disabled'`) | Postgres Flexible Server (auto-provisioned: pgvector + `mem0` db) | **Med-High** |
| Custom tools / BYO MCP (Phase 12A–D) | `AI4IA_CUSTOM_TOOLS_ENABLED=false` | `CUSTOM_TOOLS_ENABLED=false` | `customToolsEnabled=false` | Cosmos store + Key Vault (durable secrets) + **real Entra auth** | **Med-High** |
| Inline-attachment Code Interpreter (Phase 7C) | `AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED=false` | — | `inlineDocumentComputeEnabled=false` | same Responses-API code-interpreter resource as 11C + ephemeral blob container | **Med** |
| Web IQ search tools | `AI4IA_WEB_SEARCH_ENABLED=false` | — | `webSearchEnabled=false` + `webIqApiKey` secret | Microsoft Web IQ API key (or Entra managed identity) | **Low-Med** |

Already **on and usable** (for contrast): chat, push-to-talk STT + TTS, image generation
(Settings → Imagery), agents, workflows, per-session document attach.

> **Current live-env posture (`infra/main.parameters.json`).** The deployed `slurmfactory`
> env flips several of these ON: `imageGenerationEnabled`, `videoGenerationEnabled`,
> `documentUnderstandingEnabled`, `documentComputeEnabled`, `searchEnabled` (Search in
> `eastus`), `voiceLiveEnabled` + `voiceLiveToolsEnabled` (origin
> `https://ai4ia.nomad-analytics.com`), `customToolsEnabled` (BYO MCP — live behind the
> whole-app sign-in below), and **`postgresLocation=centralus`** — which derives
> `memoryStore='mem0'` and provisions the Postgres Flexible Server (real per-user memory).
> (`centralus` rather than the `eastus2` app region because the `slurmfactory` subscription is
> offer-restricted from provisioning Postgres Flexible Server in `eastus2`/`eastus`/`westus2`.)
> **Web IQ search (`webSearchEnabled`) and the inline-attachment Code Interpreter
> (`inlineDocumentComputeEnabled`) remain OFF** in the live env. The deployed app is
> **Entra-gated**: `apiAuthProvider=entra` + `appEnvironment=prod` enforce single-tenant
> sign-in (no spoofable dev auth) — required for the per-user isolation custom tools depend on.
> The "default" columns above are the **code/bicep** defaults — not the live env.

---

## 1. Voice Live (Phase 10) — *lowest-effort win*

Real-time speech-to-speech over the governed `/api/voice/live` WebSocket relay. As of the
agent-aware change, a live session adopts the **selected agent's persona + scoped tools** (the relay
is server-authoritative — the browser sends only an agent name).

**The `gpt-realtime` model is already deployed** by IaC (`infra/models.json` → `models.bicep`
provisions every catalog model by region; `gpt-realtime` v`2025-08-28` in `eastus2` +
`swedencentral`). So enabling is **config + redeploy only — no new model provision.**

**Enable (azd / Bicep params):**
```
voiceLiveEnabled       = true
realtimeAllowedOrigins = "https://<your-web-app-origin>"   # REQUIRED in any deployed env
voiceLiveToolsEnabled  = true                              # optional; gives the live agent tools
```
The live env already sets these in `infra/main.parameters.json`: `voiceLiveEnabled=true`,
`realtimeAllowedOrigins="https://ai4ia.nomad-analytics.com"` (the web vanity host), and
`voiceLiveToolsEnabled=true`. If users reach the app via the web Container App's default
`*.azurecontainerapps.io` FQDN instead of the vanity host, append that exact origin
(comma-separated) to `realtimeAllowedOrigins` — the relay matches Origin exactly (no wildcards).

This sets, on the API: `AI4IA_REALTIME_ENABLED=true`, `AI4IA_REALTIME_ALLOWED_ORIGINS`,
`AI4IA_REALTIME_TOOLS_ENABLED` (from `voiceLiveToolsEnabled`, default `false` in bicep),
`AI4IA_REALTIME_API_VERSION` (default `2025-04-01-preview`); and on the web app:
`VOICE_LIVE_ENABLED=true` + `API_PUBLIC_URL` (the API's **external** ingress — the Next.js proxy
cannot proxy WebSockets, so the browser connects to the API directly).

**Fail-closed guards (by design):**
- Realtime enabled in a deployed env with an **empty** Origin allowlist → **startup error**
  (`config.py` `validate_runtime`). An empty allowlist only "reflect-any" in `local`.
- With the flag off, the route refuses immediately and no live-voice UI is shown.

**Verify:** open a chat, pick an agent, start live voice; confirm the session speaks in the agent's
persona and only its allow-listed tools are callable.

---

## 2. Realtime tools in a live session

`AI4IA_REALTIME_TOOLS_ENABLED` (IaC `voiceLiveToolsEnabled`, default `false` in bicep; set `true`
in the live env's `main.parameters.json`) governs whether the
relay injects + executes the safe built-in tools (`calculator`, `get_current_time`) in-session.
**Inert unless Voice Live is on.** Persona injection is independent of this flag (persona works even
with live tools off). Leave on unless you specifically want a tool-free voice session.

---

## 3. Document library & multimodal understanding (Phase 11A/11B, + 11D–11F)

Per-user, cross-session document library: upload → blob → Content Understanding analyze → chunk →
index → retrieval in chat (summary cards + top-k RAG + `fetch_document` tool). Distinct from the
always-available **per-session** attach (`/api/sessions/{id}/documents`).

**Enable:**
```
documentUnderstandingEnabled = true          # API AI4IA_DOCUMENT_UNDERSTANDING_ENABLED + web DOCUMENT_LIBRARY_ENABLED
```
**Prerequisites enforced at startup outside `local` (`config.py` `validate_runtime`):**
- **Cosmos** session store (`AI4IA_SESSION_STORE=cosmos`) — the library manifest must be durable.
- **Blob account** (`AI4IA_DOCUMENT_BLOB_ACCOUNT_URL`, AAD-only; `infra` param
  `documentBlobAccountUrl`). Without it (local/dev) an in-memory blob store is used.
- **Content Understanding endpoint** (`AI4IA_CU_BASE_URL`, param `cuBaseUrl`) for enrich. Without a
  CU endpoint, ingest still works but a document stays at `stored` with its instant quick-text
  summary (no chunks/RAG). CU is its own async REST surface, **not** an OpenAI deployment.
- **AI Search (optional retrieval backend)** — set `searchEnabled=true` (param `searchLocation`
  picks the region, default `eastus`) and the API picks up `AI4IA_SEARCH_ENDPOINT`. When set, the
  Azure AI Search chunk store backs Tier-2 retrieval (hybrid vector + BM25 with optional semantic
  rerank) ahead of pgvector; unset ⇒ pgvector (or in-memory). Search is reached over the global
  `*.search.windows.net` endpoint, so it can live in a different region from the rest of the stack.

Analyzers per modality are pre-wired (`cu_{document,image,audio,video}_analyzer` = the
RAG-optimized `prebuilt-*Search` analyzers), so doc/image/audio/video ingest is supported
backend-side once a CU endpoint is configured. The library spine carries the rest of Phase 11 on
this same flag (no extra enable): **11D** audio/video time-grounding + a keyframe/scene media player
with citation deep-links, **11E** save-to-memory + owner-private annotations + erase cascade, and
**11F** document-level email sharing. **Gap:** the library UI upload path is document-centric;
non-document ingest is not yet surfaced in the UI.

---

## 4. Document compute (Phase 11C)

Intent router + sandboxed `code_interpreter` (Azure OpenAI **Responses API**) + "adjust & return"
export, layered **on top of** the library. A second default-OFF flag keeps the chat hot path
byte-for-byte unchanged unless explicitly enabled.

**Enable:**
```
documentComputeEnabled  = true               # requires documentUnderstandingEnabled = true
codeInterpreterBaseUrl  = "https://<resource>.openai.azure.com"
codeInterpreterModel    = "<deployment, e.g. gpt-4.1>"
```
Startup fails closed if compute is on without document understanding, or (outside `local`) without
the base url + model. This needs a **separate Azure OpenAI resource** that serves the Responses API
code-interpreter tool.

---

## 5. Memory / semantic recall (Phase 5)

Per-user recall of past snippets, embedded + retrieved and injected into chat (hard caps: ≤5
snippets, 500 chars each, 2000 total). The store kind **both selects the backend and gates the
feature** — there is no separate enable flag.

**Enable (live env / IaC):** set `postgresLocation` (param `AI4IA_POSTGRES_LOCATION`, default
`centralus`) to a region. A non-empty value flips `postgresEnabled` in `main.bicep`, which:
- provisions a **Postgres Flexible Server** (`Standard_B2s` Burstable) with the api managed
  identity as its **Entra admin** (no SQL passwords), the `vector` extension allowlisted, and a
  `mem0` database;
- derives `memoryStore='mem0'` and wires the api env (`AI4IA_MEMORY_STORE=mem0`,
  `AI4IA_POSTGRES_HOST/DATABASE/USER`, `MEM0_TELEMETRY=false`).

The mem0 fact-extraction (`memory_extraction_model`, a **non-reasoning** model) and embedding
(`memory_embedding_model`) calls resolve through the existing model gateway — no extra endpoint.
The server name embeds its region, so a later region change yields a fresh resource (ARM enforces
per-`resourceId` location immutability). The default is `centralus`: the live `slurmfactory`
subscription is **offer-restricted** from provisioning Postgres Flexible Server in the `eastus2`
app region (and `eastus`/`westus2`), so Postgres lives in the nearest unrestricted region. Override
`AI4IA_POSTGRES_LOCATION` if a different region is needed (verify it is unrestricted first, e.g.
`az postgres flexible-server list-skus --location <region>`).

**Enable (manual / dev):** set `AI4IA_MEMORY_STORE` directly to one of:
- `pgvector` — custom store; requires `postgres_host` + `postgres_user` (AAD role; no SQL passwords).
- `mem0` — real mem0 backend over pgvector; also runs the LLM fact-extraction pass above.
- `in_memory` — ephemeral, per-replica. **Dev only** (lost on restart/scale).
- `disabled` — off (bicep default when `postgresLocation` is empty).

**Caveats / gaps:**
- **Per-document save/forget UI shipped** (the former PR #14 is merged): ready library docs expose a
  🧠 save-to-memory and 🧽 forget control, and deleting a document cascades a forget (Phase 11E-3).
  There is still **no global toggle or "recalled memory" indicator** in chat.
- When enabled, passive memories are saved + recalled **automatically** with no per-user control.
  Consider a UX/consent pass before enabling in production.

---

## 6. Video generation (Sora-class) — *resolved*

The former "deployed-but-dark" gap is closed. An agent-callable **`generate_video`** tool (Phase
11G) now mirrors `generate_image`: a shared `VideoGenerationService` owns the Sora async
submit→poll→download job, persists the MP4 to a per-user blob, and serves it from
`GET /api/videos/artifacts/{id}`; the web renders it inline (`VideoAttachmentView`). Gated by
`videoGenerationEnabled` (default OFF in bicep, **ON** in the live env).

Image generation has both the Imagery Studio tab and an agent-callable `generate_image`; document
processing has an agent-callable `process_document` — the **image · video · document**
capability-as-tool triad is complete.

---

## 7. Custom tools / BYO remote MCP (Phase 12A–D)

Lets a signed-in user register their own **remote MCP servers** (Streamable HTTP), discover the
tools each exposes behind a strict SSRF egress guard, and attach them — by namespaced name — to
their agents for governed per-turn execution. Server secrets are stored durably in Key Vault;
the Agent Builder surfaces approval posture + per-server health/quarantine. One IaC param
(`customToolsEnabled`) drives **both** the API and the web UI.

**Enable:**
```
customToolsEnabled = true            # API: AI4IA_CUSTOM_TOOLS_ENABLED=true; web: CUSTOM_TOOLS_ENABLED=true
```
Setting the param provisions the `mcpServers` Cosmos container, grants the api managed identity
**Key Vault Secrets Officer**, and emits `AI4IA_CUSTOM_TOOLS_SECRET_VAULT_URI` to the API +
`CUSTOM_TOOLS_ENABLED=true` to the web container.

**Fail-closed prerequisites** (`validate_runtime`, enforced outside `local`): enabling custom tools
requires (a) the **Cosmos** session store (per-user server/secret isolation), (b) a **Key Vault**
URI for durable secrets, and (c) **real Entra auth** — `auth_provider=dev` is rejected because the
spoofable dev identity would break per-user tool isolation. The live env satisfies all three
(Cosmos session store, Key Vault provisioned by the same flag, whole-app Entra sign-in). When off,
the service is never built, `/api/agents/mcp-servers` returns 404, and behavior is unchanged.

---

## 8. Inline-attachment Code Interpreter (Phase 7C)

An agent-callable **`analyze_attachment`** tool that cracks an **inline composer attachment** in the
same Responses-API Code Interpreter sandbox as the library `run_code` tool, reading the **real
uploaded bytes** (PDF layout, xlsx cells, image) rather than the cheap local text extract. It
**augments** — does not replace — the instant local extract that already feeds small files into
chat context. To give the sandbox the real file, the inline upload path retains the original bytes
**ephemerally** (a dedicated blob container with a 1-day lifecycle/TTL; cleaned up on doc-delete and
session-end).

**Enable:**
```
inlineDocumentComputeEnabled = true           # AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED=true
# reuses the Responses-API code-interpreter resource from §4:
codeInterpreterBaseUrl       = "https://<resource>.openai.azure.com"
codeInterpreterModel         = "<deployment>"
# optional: AI4IA_INLINE_ATTACHMENT_BLOB_CONTAINER (default ephemeral-attachments)
```
Turning the flag on provisions the ephemeral-attachments container + its TTL rule (gated on the
document blob storage). **Fail-closed** (`validate_runtime`, outside `local`): enabling without
`code_interpreter_base_url` **and** `code_interpreter_model` raises at startup. It reuses the §4
code-interpreter settings (no second CI surface) but is **independent of the document library** — it
does not require `documentUnderstandingEnabled`. Default OFF: no original bytes are retained and the
tool is never advertised, so the inline-document path is byte-for-byte unchanged.

---

## 9. Web IQ search tools

Five agent-callable search tools — **`web_search`**, **`news_search`**, **`video_search`**,
**`image_search`**, **`browse_url`** — backed by Microsoft **Web IQ** (official `webiq` SDK, lazily
imported). They are offered on **every tool-enabled turn**, so they reach **any agent and the main
chat**. Returned web content is attacker-controlled, so every field is sanitized and **nonce-fenced**
before it reaches the model (prompt-injection defense). A shared per-turn budget caps the number of
search calls (`MAX_WEB_SEARCHES_PER_TURN=5`, a module constant), `AI4IA_WEB_SEARCH_MAX_RESULTS`
(default 5) caps results per query, and handling is fail-soft (never raises).

**Enable:**
```
webSearchEnabled = true                       # AI4IA_WEB_SEARCH_ENABLED=true
webIqApiKey      = "<Web IQ API key>"         # → Container App secret webiq-api-key → AI4IA_WEBIQ_API_KEY
# or, instead of a key:  AI4IA_WEBIQ_USE_ENTRA=true   (managed-identity auth)
# optional tunables: AI4IA_WEBIQ_BASE_URL, AI4IA_WEB_SEARCH_MAX_RESULTS,
#                    AI4IA_WEB_SEARCH_MAX_CONTENT_CHARS
```
**Fail-closed** (`validate_runtime`, outside `local`): enabling without **either** an API key **or**
`AI4IA_WEBIQ_USE_ENTRA=true` raises at startup. The secret is wired exactly like the
model-gateway/admin secrets — emitted only when `webSearchEnabled` **and** a key are present, so the
compiled ARM is empty when off. Default OFF: the factory returns `None`, no tools are advertised, and
no SDK client is constructed.

---

## Notes

- Flipping any flag to ON is a **deploy + cost** action (and, for library/compute/memory, requires
  provisioning external Azure resources). Validate in a parallel resource group first (see
  `docs/runbooks/teardown.md` §1).
- None of these defaults are changed by this runbook — it documents how to enable, not an enablement.
