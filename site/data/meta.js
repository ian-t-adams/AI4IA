// AI4IA site metadata. Hand-maintained; describes the application, its live
// environment, feature posture, and technology stack. The LIVE resource inventory
// and health live in the auto-generated inventory.js / status.js.
window.AI4IA_META = {
  name: "AI4IA",
  tagline: "A governed, multi-model, multi-region agentic chat platform on Azure.",
  description:
    "AI4IA is a multi-model, multi-region agentic chat application for personal use and " +
    "customer demos. It is a monorepo: a Next.js web app, a FastAPI backend that owns all " +
    "governance (auth, sessions, tools, memory, usage metering, entitlements, model routing), " +
    "a vendored SimpleL7Proxy model gateway, and Azure infrastructure as Bicep. Everything " +
    "deploys through the Azure Developer CLI (azd).",
  links: {
    app: "https://ai4ia.nomad-analytics.com",
    proxy: "https://genaiproxy.nomad-analytics.com",
    repo: "https://github.com/ian-t-adams/AI4IA",
    docs: "https://github.com/ian-t-adams/AI4IA/tree/main/docs",
    architectureDoc: "https://github.com/ian-t-adams/AI4IA/blob/main/docs/architecture.md",
  },
  environment: {
    envName: "slurmfactory",
    appEnvironment: "prod",
    authProvider: "Microsoft Entra ID (workforce / B2B)",
    subscription: "ca68cf94-f445-43f1-8379-3d0100e293a2",
    resourceGroup: "rg-ai4ia-slurmfactory",
    tenant: "nomad-analytics",
    primaryRegion: "East US 2",
    regions: [
      { region: "East US 2", role: "Primary US model set, realtime, image/video, router, evaluations" },
      { region: "Sweden Central", role: "EU-resident model parity where available" },
      { region: "West US", role: "Targeted models such as MAI Image and deep research" },
    ],
  },
  // Feature posture in the live (checked-in) environment. `on` reflects
  // infra/main.parameters.json + the running API container's env. Entries with a
  // `param` are cross-checked against that parameter in main.parameters.json by
  // scripts/gen-docs-catalog.py --check, so this list cannot drift from the flag
  // it advertises. (The `param` field is metadata only; the portal renderer
  // ignores it.)
  features: [
    { name: "Governed model calls", on: true, core: true,
      note: "HTTP/SSE routes SimpleL7Proxy -> APIM; Voice Live routes FastAPI relay -> APIM." },
    { name: "Microsoft Entra / MSAL authentication", on: true, core: true,
      note: "Non-spoofable identity; an internal user id is decoupled from the IdP." },
    { name: "Agents, workflows & governed tools", on: true, core: true,
      note: "Curated + user-defined agents, multi-step workflows, per-turn tool executor." },
    { name: "Per-user memory (mem0 over Postgres + pgvector)", on: true,
      note: "LLM fact-extraction + semantic recall, scoped per user." },
    { name: "Usage metering & entitlements", on: true, core: true,
      note: "Per-request token/cost ledger with per-user and default limits." },
    { name: "Voice: STT / TTS", on: true },
    { name: "Voice Live (realtime speech-to-speech relay)", on: true, param: "voiceLiveEnabled",
      note: "Browser connects to the API over WebSocket; the relay enforces governance and calls the APIM realtime API, bypassing SimpleL7Proxy." },
    { name: "Image generation", on: true, param: "imageGenerationEnabled" },
    { name: "Video generation", on: true, param: "videoGenerationEnabled" },
    { name: "Document & multimodal understanding (Content Understanding)", on: true, param: "documentUnderstandingEnabled" },
    { name: "Document compute (Responses API code interpreter)", on: true, param: "documentComputeEnabled" },
    { name: "Custom (bring-your-own) MCP tools", on: true, param: "customToolsEnabled",
      note: "Per-user MCP servers behind an SSRF guard; credentials in per-user Key Vault; approval-gated." },
    { name: "Official MCP plane (shared APIM + Foundry toolbox)", on: true,
      note: "Curated servers reached through the shared active APIM, gated on one subscription key." },
    { name: "Private tool catalog (API Center)", on: true,
      note: "Inventories the official MCP servers for governance + Foundry discovery." },
    { name: "Automatic context summarization (auto-fold)", on: true, param: "autoSummarizationEnabled" },
    { name: "Admin dashboards (usage rollups + Azure Monitor resource panels)", on: true },
    { name: "Inline-attachment code interpreter", on: true, param: "inlineDocumentComputeEnabled",
      note: "Attach a file in chat and the model runs sandboxed code over it via the same Responses API code interpreter path as document compute." },
    { name: "Web IQ search tools (web/news/videos/images/browse)", on: true, param: "webSearchEnabled",
      note: "Web/news/image/video/browse tools agents can call (approval + metering); uses the API managed identity unless AI4IA_WEBIQ_API_KEY is set." },
    { name: "Multi-app proxy profiles", on: false, param: "proxyProfilesEnabled",
      note: "Blocked until the public edge validates a workload identity and can derive a trusted app id." },
    { name: "Proxy priority reservations", on: false, param: "proxyPrioritiesEnabled",
      note: "Optional per-replica worker reservations; synchronous fairness is not durable or global." },
    { name: "Proxy Event Hub metadata export", on: false, param: "proxyEventHubTelemetryEnabled",
      note: "Routing/status/latency metadata only; prompt, response and header logging remains disabled." },
    { name: "Proxy durable async", on: false, param: "proxyAsyncEnabled",
      note: "Optional dedicated Blob + Service Bus backing; separate from the synchronous in-memory queue." },
  ],
  stack: [
    { layer: "Web", tech: "Next.js 16 · React 19 · TypeScript 6", host: "Azure Container Apps" },
    { layer: "API", tech: "FastAPI · Python 3.11+ · Pydantic", host: "Azure Container Apps" },
    { layer: "Model gateway", tech: ".NET SimpleL7Proxy -> APIM", host: "Azure Container Apps + API Management" },
    { layer: "App data", tech: "Cosmos DB (NoSQL, canonical state)", host: "Azure Cosmos DB" },
    { layer: "Memory / chunks", tech: "Postgres 16 + pgvector (mem0)", host: "Azure Database for PostgreSQL" },
    { layer: "Search", tech: "Azure AI Search (optional retrieval)", host: "Azure AI Search" },
    { layer: "Storage", tech: "Blob (documents + generated media)", host: "Azure Storage" },
    { layer: "AI services", tech: "Azure AI Foundry (3 regions) + Content Understanding", host: "Azure AI Foundry" },
    { layer: "Observability", tech: "App Insights · Log Analytics · Azure Monitor workspace", host: "Azure Monitor" },
    { layer: "IaC / deploy", tech: "Bicep + Azure Developer CLI (azd)", host: "GitHub Actions (OIDC)" },
  ],
};
