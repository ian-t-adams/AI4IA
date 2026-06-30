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
| Official MCP plane | `AI4IA_OFFICIAL_MCP_ENABLED` | none | `enableOfficialMcp` | Dedicated MCP APIM front door + ≥1 server in `infra/mcp-servers.json`; gateway URL + subscription key auto-wired |
| Web IQ search tools | `AI4IA_WEB_SEARCH_ENABLED` | none | `webSearchEnabled` | Web IQ API key or Entra managed identity outside local |
| Admin resource panels | `AI4IA_RESOURCE_METRICS_ENABLED` + resource ids | admin dashboard | resource-id env from modules | Monitoring Reader and ARM resource ids |

The checked-in live parameters currently turn on image/video generation,
document understanding, document compute, AI Search, Voice Live + tools, custom
tools, and Postgres-backed memory. Web IQ, inline attachment Code Interpreter,
and the official MCP plane remain OFF there.

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

A curated, admin-defined set of MCP servers reached **through a dedicated MCP
APIM front door** (`infra/modules/mcpgateway.bicep`, APIM Basic v2) gated on a
single app-global subscription key — distinct from per-user BYO MCP, which the
API calls directly behind the SSRF guard. Ships **empty and OFF**.

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

Provision then deploys the MCP APIM, exposes one governed MCP server per entry at
`https://<mcp-apim>/<name>/mcp`, and wires the gateway URL + subscription key into
the API (`AI4IA_OFFICIAL_MCP_GATEWAY_URL` plus a Container App secret). Startup
fails closed if the plane is enabled without both. Official servers are
**trusted** (pre-approved, no per-call human gate) and merged ahead of BYO tools
in each turn, sharing one per-turn MCP call budget.

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

## Operational reminders

- Enabling a feature is a deploy and cost action; validate in a parallel resource
  group before changing a live environment.
- `infra/main.parameters.json` documents this repo's checked-in live posture, not
  the universal defaults.
- If a feature is disabled, its route/service either refuses with 404/disabled
  semantics or is never constructed.
