// AI4IA Azure service catalog. Hand-maintained; each entry explains WHAT a deployed
// Azure resource is and WHY AI4IA uses it, plus the IaC module that creates it, the
// identity/RBAC it relies on, and first-party docs. The services page cross-references
// these against the live inventory (inventory.js) so every running resource is explained.
window.AI4IA_SERVICES = [
  {
    key: "web", name: "Web app (Container App)", azureType: "Microsoft.App/containerApps",
    group: "Compute", icon: "🌐", module: "web.bicep", resourcePattern: "ca-web-*",
    summary: "Next.js frontend serving the chat UI, admin dashboards, library/media surfaces and auth. " +
      "It proxies browser calls to the API same-origin, so the API base URL is a server-side env var never exposed to the client.",
    identity: "id-web (user-assigned) · ACR Pull to pull its image",
    docs: [["Azure Container Apps", "https://learn.microsoft.com/azure/container-apps/overview"]],
  },
  {
    key: "api", name: "API (Container App)", azureType: "Microsoft.App/containerApps",
    group: "Compute", icon: "⚙️", module: "api.bicep", resourcePattern: "ca-api-*",
    summary: "FastAPI backend and the governance brain: auth, sessions, chat, agents, tools, memory, " +
      "documents, usage metering, entitlements and all model routing. Every feature gate is enforced here.",
    identity: "id-api (user-assigned) · the most-privileged workload identity (see Requirements → Permissions)",
    docs: [["FastAPI", "https://fastapi.tiangolo.com/"], ["Azure Container Apps", "https://learn.microsoft.com/azure/container-apps/overview"]],
  },
  {
    key: "proxy", name: "Model proxy (Container App)", azureType: "Microsoft.App/containerApps",
    group: "Compute", icon: "🛡️", module: "gateway.bicep", resourcePattern: "ca-proxy-*",
    summary: "Public HTTP/SSE gateway edge. Applications call SimpleL7Proxy first for queueing, priority and delayed " +
      "requeue; its only model backend is APIM. Realtime WebSockets bypass it through the FastAPI relay.",
    identity: "id-proxy (user-assigned) · ACR Pull + App Configuration reader; optional telemetry/async roles",
    docs: [["SimpleL7Proxy (upstream)", "https://github.com/microsoft/SimpleL7Proxy"]],
  },
  {
    key: "cae", name: "Container Apps Environment", azureType: "Microsoft.App/managedEnvironments",
    group: "Compute", icon: "🏗️", module: "containerapps.bicep", resourcePattern: "cae-ai4ia-*",
    summary: "The managed hosting environment for the web/api/proxy container apps, wired to Log Analytics. " +
      "Also holds the managed TLS certificates for the web and proxy custom domains.",
    identity: "n/a",
    docs: [["Container Apps environment", "https://learn.microsoft.com/azure/container-apps/environment"]],
  },
  {
    key: "acr", name: "Container Registry", azureType: "Microsoft.ContainerRegistry/registries",
    group: "Compute", icon: "📦", module: "containerapps.bicep", resourcePattern: "acrai4ia*",
    summary: "Stores the web/api/proxy container images that azd builds and pushes. The three workload " +
      "identities are granted ACR Pull to deploy from it.",
    identity: "grants ACR Pull to id-web / id-api / id-proxy",
    docs: [["Azure Container Registry", "https://learn.microsoft.com/azure/container-registry/container-registry-intro"]],
  },
  {
    key: "apim-mcp", name: "API Management — AI gateway", azureType: "Microsoft.ApiManagement/service",
    group: "Gateway", icon: "🔌", module: "apimcore.bicep", resourcePattern: "apim-mcp-ai4ia-*",
    summary: "The single APIM service (Basic v2) that exposes and governs the curated 'official' " +
      "MCP servers using APIM's native MCP feature, gated on one subscription key, while also hosting the " +
      "model and realtime APIs. MCP, HTTP/SSE, and both voice providers share it.",
    identity: "system-assigned; may be granted Foundry User to mint the toolbox bearer",
    docs: [["Expose MCP servers in APIM", "https://learn.microsoft.com/azure/api-management/export-rest-mcp-server"]],
  },
  {
    key: "foundry", name: "Azure AI Foundry accounts (×3)", azureType: "Microsoft.CognitiveServices/accounts",
    group: "AI", icon: "🧠", module: "foundry.bicep + models.bicep", resourcePattern: "mf-aiforia-slurmfactory-{region}",
    summary: "Azure AI Services (Foundry) accounts in East US 2, Sweden Central and West US, each with a default " +
      "project. They serve chat/embedding/realtime/speech/image/video models (deployed from infra/models.json) and " +
      "Content Understanding. APIM reaches normal model deployments with MI; FastAPI retains direct MI only for native/control planes such as CU and code interpreter.",
    identity: "APIM system MI and the API native-plane identity hold scoped data-plane roles; the proxy has no Foundry RBAC",
    docs: [["Azure AI Foundry", "https://learn.microsoft.com/azure/ai-foundry/"], ["Content Understanding", "https://learn.microsoft.com/azure/ai-services/content-understanding/"]],
  },
  {
    key: "cosmos", name: "Cosmos DB (NoSQL)", azureType: "Microsoft.DocumentDB/databaseAccounts",
    group: "Data", icon: "🗄️", module: "data.bicep", resourcePattern: "cosmos-ai4ia-*",
    summary: "Canonical application state: sessions, messages, usage ledger, user agents/workflows, MCP server " +
      "records and document manifests. AAD-only via managed identity + the built-in Cosmos Data Contributor role — no keys.",
    identity: "id-api granted Cosmos DB Built-in Data Contributor (data plane)",
    docs: [["Azure Cosmos DB", "https://learn.microsoft.com/azure/cosmos-db/introduction"]],
  },
  {
    key: "postgres", name: "Postgres Flexible Server (pgvector)", azureType: "Microsoft.DBforPostgreSQL/flexibleServers",
    group: "Data", icon: "🧬", module: "data.bicep", resourcePattern: "psql-ai4ia-*",
    summary: "PostgreSQL 16 with pgvector — retained for document chunk vectors and as the legacy memory-migration source; per-user memory is now canonical in Cosmos DB. " +
      "Passwordless: the API authenticates as its managed identity with an AAD access token (no SQL password).",
    identity: "id-api authenticates via Entra token (AI4IA_POSTGRES_USER = id-api)",
    docs: [["Postgres Flexible Server", "https://learn.microsoft.com/azure/postgresql/flexible-server/"], ["pgvector", "https://github.com/pgvector/pgvector"]],
  },
  {
    key: "search", name: "Azure AI Search", azureType: "Microsoft.Search/searchServices",
    group: "Data", icon: "🔎", module: "search.bicep", resourcePattern: "srch-ai4ia-*",
    summary: "Optional hybrid/semantic retrieval backend for the document chunk index. AAD-only: the API reaches the " +
      "data plane through its managed identity with Search Index Data Contributor + Search Service Contributor — no admin keys.",
    identity: "id-api granted Search Index Data Contributor + Search Service Contributor",
    docs: [["Azure AI Search", "https://learn.microsoft.com/azure/search/search-what-is-azure-search"]],
  },
  {
    key: "storage", name: "Storage accounts (×2 + optional async)", azureType: "Microsoft.Storage/storageAccounts",
    group: "Data", icon: "💾", module: "data.bicep + proxyasync.bicep", resourcePattern: "st*",
    summary: "Blob storage for raw + parsed documents (library) and for generated media (images/videos). Identity-based " +
      "access only. Durable proxy async adds a dedicated default-off account; the two application accounts emit Event Grid topics.",
    identity: "id-api owns application blobs; id-proxy gets Blob Data Contributor only on the optional async account",
    docs: [["Azure Blob Storage", "https://learn.microsoft.com/azure/storage/blobs/storage-blobs-introduction"]],
  },
  {
    key: "keyvault", name: "Key Vault", azureType: "Microsoft.KeyVault/vaults",
    group: "Security", icon: "🔐", module: "keyvault.bicep", resourcePattern: "kvai4ia*",
    summary: "RBAC-mode Key Vault holding per-user (BYO) MCP connection credentials. Only opaque references land in " +
      "Cosmos; APIM hop keys are separate Container App secrets and are not stored here.",
    identity: "app identities get Secrets User (read); id-api gets Secrets Officer when custom tools are enabled (write)",
    docs: [["Azure Key Vault", "https://learn.microsoft.com/azure/key-vault/general/overview"]],
  },
  {
    key: "appconfig", name: "App Configuration", azureType: "Microsoft.AppConfiguration/configurationStores",
    group: "Config", icon: "🎛️", module: "keyvault.bicep", resourcePattern: "appcs-ai4ia-*",
    summary: "Centralized configuration store paired with Key Vault. App identities read it with the App Configuration " +
      "Data Reader role.",
    identity: "app identities granted App Configuration Data Reader",
    docs: [["Azure App Configuration", "https://learn.microsoft.com/azure/azure-app-configuration/overview"]],
  },
  {
    key: "apicenter", name: "API Center (private tool catalog)", azureType: "Microsoft.ApiCenter/services",
    group: "Gateway", icon: "📇", module: "apicenter.bicep", resourcePattern: "apic-ai4ia-*",
    summary: "Inventories the 'official' MCP servers already fronted by the shared APIM so they are discoverable and " +
      "governable as one private tool catalog — the same URLs Foundry agents can discover, with no second auth path.",
    identity: "n/a (populated by scripts/provision-private-tool-catalog.py)",
    docs: [["Azure API Center", "https://learn.microsoft.com/azure/api-center/overview"]],
  },
  {
    key: "eventhubs", name: "Event Hubs Namespace", azureType: "Microsoft.EventHub/namespaces",
    group: "Messaging", icon: "📨", module: "eventhubs.bicep", resourcePattern: "evhns-ai4ia-*",
    summary: "Telemetry/eventing backbone for cost/usage events. Identity-based auth only (local/SAS auth disabled); " +
      "senders/receivers use the Event Hubs Data Sender/Receiver roles.",
    identity: "id-api sender/receiver; id-proxy sender only when proxy telemetry is enabled",
    docs: [["Azure Event Hubs", "https://learn.microsoft.com/azure/event-hubs/event-hubs-about"]],
  },
  {
    key: "servicebus", name: "Service Bus Namespace (optional async)", azureType: "Microsoft.ServiceBus/namespaces",
    group: "Messaging", icon: "📬", module: "proxyasync.bicep", resourcePattern: "sb-ai4ia-*-proxy-async",
    summary: "Default-off durable async request queue for SimpleL7Proxy. It is separate from the synchronous in-memory " +
      "priority queue, disables local auth, follows the private-data-tier posture, and sends diagnostics to Log Analytics.",
    identity: "id-proxy granted Service Bus Data Sender + Receiver only when async is enabled",
    docs: [["Azure Service Bus", "https://learn.microsoft.com/azure/service-bus-messaging/service-bus-messaging-overview"]],
  },
  {
    key: "eventgrid", name: "Event Grid System Topics (×2)", azureType: "Microsoft.EventGrid/systemTopics",
    group: "Messaging", icon: "⚡", module: "data.bicep (implicit)", resourcePattern: "st*-{guid}",
    summary: "Auto-created system topics that surface blob change events from the two storage accounts (e.g. to drive " +
      "asynchronous document ingestion).",
    identity: "n/a",
    docs: [["Event Grid system topics", "https://learn.microsoft.com/azure/event-grid/system-topics"]],
  },
  {
    key: "appinsights", name: "Application Insights", azureType: "Microsoft.Insights/components",
    group: "Observability", icon: "📈", module: "monitoring.bicep", resourcePattern: "appi-ai4ia-*",
    summary: "Distributed tracing, request/dependency/exception telemetry and custom usage events, exported by the API's " +
      "Azure Monitor OpenTelemetry distro (only when a connection string is set, so local/dev is a no-op). Its auto-created " +
      "Smart Detection action group also lives in the resource group.",
    identity: "n/a (connection-string gated)",
    docs: [["Application Insights", "https://learn.microsoft.com/azure/azure-monitor/app/app-insights-overview"]],
  },
  {
    key: "loganalytics", name: "Log Analytics Workspace", azureType: "Microsoft.OperationalInsights/workspaces",
    group: "Observability", icon: "🪵", module: "monitoring.bicep", resourcePattern: "log-ai4ia-*",
    summary: "Central log sink for the Container Apps environment and diagnostic settings across the stack, and the " +
      "backing store for Application Insights.",
    identity: "n/a",
    docs: [["Log Analytics", "https://learn.microsoft.com/azure/azure-monitor/logs/log-analytics-overview"]],
  },
  {
    key: "monitorworkspace", name: "Azure Monitor Workspace", azureType: "Microsoft.Monitor/accounts",
    group: "Observability", icon: "📊", module: "monitoring.bicep", resourcePattern: "amw-ai4ia-*",
    summary: "Managed Prometheus metrics store. The admin dashboard's resource panels read Azure Monitor platform " +
      "metrics via the batch metrics API (which requires Monitoring Reader at subscription scope).",
    identity: "id-api granted Monitoring Reader at SUBSCRIPTION scope",
    docs: [["Azure Monitor workspace", "https://learn.microsoft.com/azure/azure-monitor/essentials/azure-monitor-workspace-overview"]],
  },
  {
    key: "identities", name: "User-Assigned Managed Identities (×3)", azureType: "Microsoft.ManagedIdentity/userAssignedIdentities",
    group: "Security", icon: "🪪", module: "identity.bicep", resourcePattern: "id-{web,api,proxy}-*",
    summary: "The workload identities for the web, api and proxy container apps. Azure service data planes " +
      "(Cosmos, Storage, Search, Key Vault, Postgres, Foundry, Monitor) use these identities + RBAC; scoped APIM hop keys remain Container App secrets.",
    identity: "see Requirements → Permissions for the full role map",
    docs: [["Managed identities", "https://learn.microsoft.com/azure/active-directory/managed-identities-azure-resources/overview"]],
  },
];
