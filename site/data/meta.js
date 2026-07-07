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
  // infra/main.parameters.json + the running API container's env.
  features: [
    { name: "Governed model calls (APIM + SimpleL7Proxy)", on: true, core: true,
      note: "All chat, embeddings, speech, image/video and realtime models route through the gateway." },
    { name: "Microsoft Entra / MSAL authentication", on: true, core: true,
      note: "Non-spoofable identity; an internal user id is decoupled from the IdP." },
    { name: "Agents, workflows & governed tools", on: true, core: true,
      note: "Curated + user-defined agents, multi-step workflows, per-turn tool executor." },
    { name: "Per-user memory (mem0 over Postgres + pgvector)", on: true,
      note: "LLM fact-extraction + semantic recall, scoped per user." },
    { name: "Usage metering & entitlements", on: true, core: true,
      note: "Per-request token/cost ledger with per-user and default limits." },
    { name: "Voice: STT / TTS", on: true },
    { name: "Voice Live (realtime speech-to-speech relay)", on: true,
      note: "Browser connects directly to the API over WebSocket; the relay still enforces auth, Origin, entitlements and metering." },
    { name: "Image generation", on: true },
    { name: "Video generation", on: true },
    { name: "Document & multimodal understanding (Content Understanding)", on: true },
    { name: "Document compute (Responses API code interpreter)", on: true },
    { name: "Custom (bring-your-own) MCP tools", on: true,
      note: "Per-user MCP servers behind an SSRF guard; credentials in per-user Key Vault; approval-gated." },
    { name: "Official MCP plane (MCP APIM front door + Foundry toolbox)", on: true,
      note: "Curated servers reached through a dedicated APIM, gated on one subscription key." },
    { name: "Private tool catalog (API Center)", on: true,
      note: "Inventories the official MCP servers for governance + Foundry discovery." },
    { name: "Automatic context summarization (auto-fold)", on: true },
    { name: "Admin dashboards (usage rollups + Azure Monitor resource panels)", on: true },
    { name: "Inline-attachment code interpreter", on: false,
      note: "Implemented; OFF in the checked-in live parameters." },
    { name: "Web IQ search tools (web/news/videos/images/browse)", on: false,
      note: "Implemented; OFF in the checked-in live parameters." },
  ],
  stack: [
    { layer: "Web", tech: "Next.js 16 · React 19 · TypeScript 6", host: "Azure Container Apps" },
    { layer: "API", tech: "FastAPI · Python 3.11+ · Pydantic", host: "Azure Container Apps" },
    { layer: "Model gateway", tech: ".NET SimpleL7Proxy behind APIM", host: "Azure Container Apps + API Management" },
    { layer: "App data", tech: "Cosmos DB (NoSQL, canonical state)", host: "Azure Cosmos DB" },
    { layer: "Memory / chunks", tech: "Postgres 16 + pgvector (mem0)", host: "Azure Database for PostgreSQL" },
    { layer: "Search", tech: "Azure AI Search (optional retrieval)", host: "Azure AI Search" },
    { layer: "Storage", tech: "Blob (documents + generated media)", host: "Azure Storage" },
    { layer: "AI services", tech: "Azure AI Foundry (3 regions) + Content Understanding", host: "Azure AI Foundry" },
    { layer: "Observability", tech: "App Insights · Log Analytics · Azure Monitor workspace", host: "Azure Monitor" },
    { layer: "IaC / deploy", tech: "Bicep + Azure Developer CLI (azd)", host: "GitHub Actions (OIDC)" },
  ],
};
