// AI4IA site metadata. Hand-maintained; describes the application, its deployed
// environment, feature posture, and technology stack. The timestamped resource
// inventory and health snapshot live in auto-generated inventory.js / status.js.
window.AI4IA_META = {
  name: "AI4IA",
  tagline: "A governed, multi-model, multi-region agentic chat platform on Azure.",
  description:
    "AI4IA provides governed multimodal and agent chat for enterprise knowledge work while " +
    "showcasing Azure capabilities. It is a monorepo: a Next.js web app, a FastAPI backend that owns all " +
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
    subscription: "e852113b-6cb5-441c-ac68-26cff884e479",
    resourceGroup: "rg-ai4ia-slurmfactory",
    // Directory display name as Entra actually reports it. The intended name is
    // "Planet Express" (already what the subscriptions are named), but renaming a
    // tenant is a directory write and this account is only Owner at the ARM root
    // management group -- two separate permission planes. Purely cosmetic.
    tenant: "Contoso",
    primaryRegion: "East US 2",
    regions: [
      { region: "East US 2", role: "Primary US model set, realtime, image/video, router, evaluations" },
      { region: "Sweden Central", role: "EU-resident model parity where available" },
      { region: "West US", role: "Targeted models such as MAI Image and deep research" },
    ],
  },
  featurePosture: {
    templateSource: "infra/main.parameters.json placeholder defaults",
    observedSource: "README and feature-enablement production evidence",
    observedAt: "2026-08-09",
    caveat:
      "CI validates templateOn against repository defaults. observedOn is a dated, hand-maintained production observation; CI does not query live Azure.",
  },
  // `templateOn` is mechanically checked against main.parameters.json.
  // `observedOn` is deliberately separate and dated above; a repository or azd
  // variable can override the template without changing the checked-in default.
  features: [
    { name: "Governed model calls", templateOn: true, observedOn: true, core: true,
      note: "HTTP/SSE routes SimpleL7Proxy -> APIM; Voice Live routes FastAPI relay -> APIM." },
    { name: "Microsoft Entra / MSAL authentication", templateOn: true, observedOn: true, core: true,
      note: "Non-spoofable identity; an internal user id is decoupled from the IdP." },
    { name: "Agents, workflows & governed tools", templateOn: true, observedOn: true, core: true,
      note: "Curated + user-defined agents, multi-step workflows, per-turn tool executor." },
    { name: "Durable workflow execution", templateOn: true, observedOn: true, param: "enableDurableWorkflows",
      note: "Opt-in runs use Azure Durable Task Scheduler; synchronous runs remain available." },
    { name: "Per-user memory (Cosmos DB vectors)", templateOn: true, observedOn: true,
      note: "LLM fact-extraction + semantic recall, scoped per user." },
    { name: "Usage metering & entitlements", templateOn: true, observedOn: true, core: true,
      note: "Per-request token/cost ledger with per-user and default limits." },
    { name: "Voice: STT / TTS", templateOn: true, observedOn: true },
    { name: "Azure OpenAI Voice Live", templateOn: true, observedOn: true, param: "voiceLiveEnabled",
      note: "Live, but modality-scope RAI re-approval is not evidenced in the decision record." },
    { name: "Speech Voice Live", templateOn: false, observedOn: true, param: "speechVoiceLiveEnabled",
      note: "Production override is on; template default is off. Modality-scope RAI re-approval is not evidenced." },
    { name: "Image generation", templateOn: true, observedOn: true, param: "imageGenerationEnabled",
      note: "Live; modality-scope RAI re-approval is not evidenced in the decision record." },
    { name: "Video generation", templateOn: true, observedOn: true, param: "videoGenerationEnabled",
      note: "Live; modality-scope RAI re-approval is not evidenced in the decision record." },
    { name: "Document & multimodal understanding (Content Understanding)", templateOn: true, observedOn: true, param: "documentUnderstandingEnabled" },
    { name: "Document compute (Responses API code interpreter)", templateOn: true, observedOn: true, param: "documentComputeEnabled" },
    { name: "Custom (bring-your-own) MCP tools", templateOn: true, observedOn: true, param: "customToolsEnabled",
      note: "Per-user MCP servers behind an SSRF guard; credentials in per-user Key Vault; approval-gated." },
    { name: "Official MCP plane (shared APIM + Foundry toolbox)", templateOn: true, observedOn: true,
      note: "Curated discovery through APIM; interactive invocation approval still applies." },
    { name: "Private tool catalog (API Center)", templateOn: true, observedOn: true,
      note: "IaC-owned MCP assets deploy the governed APIM consumer URLs for Foundry discovery." },
    { name: "Automatic context summarization (auto-fold)", templateOn: true, observedOn: true, param: "autoSummarizationEnabled" },
    { name: "Admin dashboards (usage rollups + Azure Monitor resource panels)", templateOn: true, observedOn: true },
    { name: "Inline-attachment code interpreter", templateOn: true, observedOn: true, param: "inlineDocumentComputeEnabled",
      note: "Attach a file in chat and the model runs sandboxed code over it via the same Responses API code interpreter path as document compute." },
    { name: "Web IQ search tools (web/news/videos/images/browse)", templateOn: true, observedOn: true, param: "webSearchEnabled",
      note: "Web/news/image/video/browse tools agents can call (approval + metering); uses the API managed identity unless AI4IA_WEBIQ_API_KEY is set." },
    { name: "Multi-app proxy profiles", templateOn: false, observedOn: false, param: "proxyProfilesEnabled",
      note: "Blocked until the public edge validates a workload identity and can derive a trusted app id." },
    { name: "Proxy priority reservations", templateOn: false, observedOn: true, param: "proxyPrioritiesEnabled",
      note: "Production repository override is on; template default is off. Reservations are per replica." },
    { name: "Proxy Event Hub metadata export", templateOn: false, observedOn: false, param: "proxyEventHubTelemetryEnabled",
      note: "Routing/status/latency metadata only; prompt, response and header logging remains disabled." },
    { name: "Proxy durable async", templateOn: false, observedOn: false, param: "proxyAsyncEnabled",
      note: "Optional dedicated Blob + Service Bus backing; separate from the synchronous in-memory queue." },
  ],
  stack: [
    { layer: "Web", tech: "Next.js 16 · React 19 · TypeScript 6", host: "Azure Container Apps" },
    { layer: "API", tech: "FastAPI · Python 3.11+ · Pydantic", host: "Azure Container Apps" },
    { layer: "Model gateway", tech: ".NET SimpleL7Proxy -> APIM", host: "Azure Container Apps + API Management" },
    { layer: "App data", tech: "Cosmos DB (NoSQL, canonical state)", host: "Azure Cosmos DB" },
    { layer: "Memory / chunks", tech: "Cosmos DB vectors (canonical memory); Azure AI Search (document chunks)", host: "Azure Cosmos DB · Azure AI Search" },
    { layer: "Search", tech: "Azure AI Search (optional retrieval)", host: "Azure AI Search" },
    { layer: "Storage", tech: "Blob (documents + generated media)", host: "Azure Storage" },
    { layer: "AI services", tech: "Azure AI Foundry (3 regions) + Content Understanding", host: "Azure AI Foundry" },
    { layer: "Observability", tech: "App Insights · Log Analytics", host: "Azure Monitor" },
    { layer: "IaC / deploy", tech: "Bicep + Azure Developer CLI (azd)", host: "GitHub Actions (OIDC)" },
  ],
};
