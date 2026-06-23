// FastAPI backend (app/api) running on Container Apps.
// azd builds app/api/Dockerfile, pushes to ACR, and deploys into this app
// (matched by the `azd-service-name: api` tag).
@description('Location for the api container app.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Container Apps managed environment resource ID.')
param containerEnvId string

@description('Central Log Analytics workspace resource ID for diagnostic settings.')
param logAnalyticsWorkspaceId string

@description('Resource ID of the api user-assigned identity.')
param apiIdentityResourceId string

@description('Client ID of the api user-assigned identity (for AZURE_CLIENT_ID / Managed Identity auth).')
param apiIdentityClientId string

@description('Principal ID (objectId) of the api user-assigned identity, for resource-scoped role assignments (Monitoring Reader on the container app).')
param apiIdentityPrincipalId string

@description('ACR login server the api image is pulled from.')
param acrLoginServer string

@description('Container image for the api; azd replaces the default with the built app/api image.')
param apiImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Model gateway base URL (APIM front door + /openai).')
param modelGatewayUrl string

@description('Inbound auth mode the api uses when calling the model gateway (none|api_key|bearer). Must not be none in prod.')
@allowed([
  'none'
  'api_key'
  'bearer'
])
param modelGatewayAuthMode string = 'none'

@description('Model gateway API key (only used when modelGatewayAuthMode == api_key). Stored as a Container App secret.')
@secure()
param modelGatewayApiKey string = ''

@description('Cosmos DB account endpoint for the canonical session store.')
param cosmosEndpoint string

@description('Cosmos DB database name.')
param cosmosDatabase string

@description('Memory store backend the api uses (disabled|in_memory|pgvector|mem0).')
@allowed([
  'disabled'
  'in_memory'
  'pgvector'
  'mem0'
])
param memoryStore string = 'disabled'

@description('Postgres Flexible Server FQDN for the pgvector memory store (empty when memory is not pgvector).')
param postgresHost string = ''

@description('Postgres database name for the pgvector memory store.')
param postgresDatabase string = 'mem0'

@description('Postgres AAD role name the api identity connects as (its identity resource name).')
param postgresUser string = ''

@description('Application Insights connection string for api telemetry.')
param appInsightsConnectionString string

@description('Application runtime environment (maps to AI4IA_ENV). One of local|dev|prod.')
@allowed([
  'dev'
  'prod'
])
param appEnvironment string = 'dev'

@description('Auth provider the api enforces (dev|entra).')
@allowed([
  'dev'
  'entra'
])
param authProvider string = 'dev'

@description('Permit the dev auth provider outside local (set true only for non-prod demos without Entra).')
param allowDevAuth bool = true

@description('Entra tenant ID (required when authProvider == entra).')
param entraTenantId string = ''

@description('Entra audience / API app ID URI (required when authProvider == entra).')
param entraAudience string = ''

@description('Comma-separated admin subjects for the entitlement-management API (AI4IA_ADMIN_SUBJECTS).')
param adminSubjects string = ''

@description('Shared secret required for the entitlement-management API under spoofable dev auth (AI4IA_ADMIN_API_SECRET). Stored as a Container App secret.')
@secure()
param adminApiSecret string = ''

@description('Enable the Voice Live realtime WebSocket relay. Default OFF (the /api/voice/live route refuses, so the app is inert).')
param realtimeEnabled bool = false

@description('Azure OpenAI realtime api-version the relay uses for the upstream WebSocket.')
param realtimeApiVersion string = '2025-04-01-preview'

@description('Comma-separated browser Origin allowlist for the live-voice relay handshake. Required (non-empty) when realtimeEnabled in a deployed env (the relay fails closed otherwise).')
param realtimeAllowedOrigins string = ''

@description('Enable governed tool calling inside a live session (the relay injects the safe built-in tools and executes the model\'s function calls in-process). Inert unless realtimeEnabled is also true.')
param realtimeToolsEnabled bool = true

@description('Enable the per-user document library and multimodal understanding. Default OFF (the /api/library API refuses with 404 and nothing is constructed, so the app is inert).')
param documentUnderstandingEnabled bool = false

@description('Blob account URL backing the document library raw + parsed + chunk artifacts (empty until document storage is provisioned).')
param documentBlobAccountUrl string = ''

@description('Blob container the document library writes to.')
param documentBlobContainer string = 'documents'

@description('Content Understanding endpoint base URL (e.g. the AI Services / Foundry account or the gateway fronting it). Required when enabling document understanding in a deployed env; the api fails closed at startup otherwise.')
param cuBaseUrl string = ''

@description('Content Understanding REST api-version the ingest worker uses.')
param cuApiVersion string = '2025-11-01'

@description('Enable compute over the library: intent router + code_interpreter + "adjust & return" export. Layered ON TOP of documentUnderstandingEnabled. Default OFF (the chat hot path is byte-for-byte unchanged; neither synthetic tool is advertised).')
param documentComputeEnabled bool = false

@description('Azure OpenAI resource endpoint (e.g. https://<resource>.openai.azure.com) that serves the Responses API code_interpreter tool. Required when enabling document compute in a deployed env; the api fails closed at startup otherwise.')
param codeInterpreterBaseUrl string = ''

@description('Deployment/model name that serves the Responses API code_interpreter tool (e.g. gpt-4.1). Required when enabling document compute in a deployed env.')
param codeInterpreterModel string = ''

@description('Enable the inline-attachment code interpreter (analyze_attachment): the chat agent can crack/analyze an INLINE composer attachment in the Responses API code_interpreter sandbox, reusing the same endpoint/model as document compute. Default OFF: no original bytes retained, the tool is never advertised, no ephemeral container env is emitted — the chat hot path is byte-for-byte unchanged.')
param inlineDocumentComputeEnabled bool = false

@description('Dedicated short-lived blob container holding inline-attachment original bytes (inline code interpreter). Emitted as AI4IA_INLINE_ATTACHMENT_BLOB_CONTAINER only when the feature is on.')
param inlineAttachmentBlobContainer string = 'ephemeral-attachments'

@description('Enable the agent-callable generate_image tool. Default OFF. When on (and an image blob account is provisioned) any agent may attach generate_image; produced images persist to dedicated blob storage and serve through an authenticated endpoint.')
param imageGenerationEnabled bool = false

@description('Blob account URL backing tool-generated images (empty until image storage is provisioned). When empty the api falls back to an in-memory store.')
param imageBlobAccountUrl string = ''

@description('Blob container tool-generated images are written to.')
param imageBlobContainer string = 'images'

@description('Enable the agent-callable generate_video tool. Default OFF. When on (and a video blob account is provisioned) any agent may attach generate_video; produced clips persist to blob storage and serve through an authenticated endpoint.')
param videoGenerationEnabled bool = false

@description('Blob account URL backing tool-generated videos (empty until video storage is provisioned). When empty the api falls back to an in-memory store.')
param videoBlobAccountUrl string = ''

@description('Blob container tool-generated videos are written to.')
param videoBlobContainer string = 'videos'

@description('Azure AI Search endpoint (e.g. https://<svc>.search.windows.net). Empty unless a search service is provisioned; when set, emitted as AI4IA_SEARCH_ENDPOINT so the api can index/query via managed identity.')
param searchEndpoint string = ''

@description('ARM resource id of the Azure AI Search service for the admin Search resource panel (empty when search is not deployed).')
param metricsSearchResourceId string = ''

@description('ARM resource id of the Postgres flexible server for the admin Postgres resource panel (empty when Postgres is not deployed).')
param metricsPostgresResourceId string = ''

@description('ARM resource id of the Cosmos DB account for the admin Cosmos resource panel.')
param metricsCosmosResourceId string = ''

@description('Enable custom tools / bring-your-own MCP servers. Default OFF: the per-user MCP registry is never constructed and /api/agents/mcp-servers refuses (404). When on, users register remote MCP servers behind the SSRF guard.')
param customToolsEnabled bool = false

@description('Key Vault URI backing durable MCP connection secrets. Set only when custom tools is enabled; the api managed identity holds Key Vault Secrets Officer on this vault. Empty leaves the api on its in-memory secret store.')
param customToolsKeyVaultUri string = ''

@description('Enable the agent-callable Web IQ search tools (web/news/videos/images/browse). Default OFF: no SDK client is constructed and no web tool is advertised. When on, supply webIqApiKey (stored as a Container App secret) or rely on managed-identity EntraID.')
param webSearchEnabled bool = false

@description('Web IQ API key (only used when webSearchEnabled). Stored as a Container App secret and surfaced as AI4IA_WEBIQ_API_KEY. Empty falls back to EntraID (DefaultAzureCredential).')
@secure()
param webIqApiKey string = ''

@description('Optional Web IQ base URL override. Emitted as AI4IA_WEBIQ_BASE_URL only when webSearchEnabled and set; empty uses the SDK default endpoint.')
param webIqBaseUrl string = ''

var entraEnv = authProvider == 'entra' ? [
  {
    name: 'AI4IA_ENTRA_TENANT_ID'
    value: entraTenantId
  }
  {
    name: 'AI4IA_ENTRA_AUDIENCE'
    value: entraAudience
  }
] : []

// Gateway API key is held as a Container App secret and referenced by env when present.
var hasGatewayKey = !empty(modelGatewayApiKey)
var gatewaySecrets = hasGatewayKey ? [
  {
    name: 'model-gateway-api-key'
    value: modelGatewayApiKey
  }
] : []
var gatewayKeyEnv = hasGatewayKey ? [
  {
    name: 'AI4IA_MODEL_GATEWAY_API_KEY'
    secretRef: 'model-gateway-api-key'
  }
] : []

// Web IQ search (default-OFF). The API key is held as a Container App secret and
// referenced by env only when web search is enabled AND a key is supplied;
// otherwise the api falls back to EntraID (managed identity) when the feature is
// on, or stays fully dormant when off (no secret, no env — byte-for-byte inert).
var hasWebIqKey = webSearchEnabled && !empty(webIqApiKey)
var webIqSecrets = hasWebIqKey ? [
  {
    name: 'webiq-api-key'
    value: webIqApiKey
  }
] : []
var webSearchEnv = concat(
  webSearchEnabled ? [
    {
      name: 'AI4IA_WEB_SEARCH_ENABLED'
      value: 'true'
    }
  ] : [],
  hasWebIqKey ? [
    {
      name: 'AI4IA_WEBIQ_API_KEY'
      secretRef: 'webiq-api-key'
    }
  ] : [],
  (webSearchEnabled && !empty(webIqBaseUrl)) ? [
    {
      name: 'AI4IA_WEBIQ_BASE_URL'
      value: webIqBaseUrl
    }
  ] : []
)

// Admin API secret (entitlement management) held as a Container App secret and
// referenced by env when present. Optional: empty means identity-only admin.
var hasAdminSecret = !empty(adminApiSecret)
var adminSecrets = hasAdminSecret ? [
  {
    name: 'admin-api-secret'
    value: adminApiSecret
  }
] : []
var adminEnv = concat(
  empty(adminSubjects) ? [] : [
    {
      name: 'AI4IA_ADMIN_SUBJECTS'
      value: adminSubjects
    }
  ],
  hasAdminSecret ? [
    {
      name: 'AI4IA_ADMIN_API_SECRET'
      secretRef: 'admin-api-secret'
    }
  ] : []
)

// Postgres connection is required for the durable memory backends (the custom
// pgvector store and the real-mem0 store both persist vectors in Postgres).
var pgEnv = (memoryStore == 'pgvector' || memoryStore == 'mem0') ? [
  {
    name: 'AI4IA_POSTGRES_HOST'
    value: postgresHost
  }
  {
    name: 'AI4IA_POSTGRES_DATABASE'
    value: postgresDatabase
  }
  {
    name: 'AI4IA_POSTGRES_USER'
    value: postgresUser
  }
] : []
// mem0-specific: disable the library's PostHog telemetry at the process level
// (belt-and-suspenders; the code also setdefault()s this before importing mem0).
var mem0Env = memoryStore == 'mem0' ? [
  {
    name: 'MEM0_TELEMETRY'
    value: 'false'
  }
] : []
var memoryEnv = concat([
  {
    name: 'AI4IA_MEMORY_STORE'
    value: memoryStore
  }
], pgEnv, mem0Env)

// Voice Live realtime relay settings. Default OFF: with the flag unset
// the /api/voice/live WebSocket refuses immediately, so the relay is inert and the
// app's default behavior is unchanged. When enabled, the relay reuses the same
// model-gateway URL + credential as chat for the upstream realtime socket; only
// the flag, api-version, and Origin allowlist are realtime-specific env.
var realtimeEnv = realtimeEnabled ? [
  {
    name: 'AI4IA_REALTIME_ENABLED'
    value: 'true'
  }
  {
    name: 'AI4IA_REALTIME_API_VERSION'
    value: realtimeApiVersion
  }
  {
    name: 'AI4IA_REALTIME_ALLOWED_ORIGINS'
    value: realtimeAllowedOrigins
  }
  {
    name: 'AI4IA_REALTIME_TOOLS_ENABLED'
    value: realtimeToolsEnabled ? 'true' : 'false'
  }
] : []

// Document understanding settings. Default OFF: the library repo
// + ingest pipeline are not constructed and the /api/library API refuses (404),
// so the feature is inert. When enabled, the blob env points the ingest path at
// the provisioned (AAD-only) storage account; the CU env is emitted only when a
// Content Understanding endpoint is supplied (without it, enrich is a no-op and a
// document stays at `stored` with its instant quick-text summary). The Cosmos
// containers it uses (userDocuments, analyzers) are created unconditionally by the
// data module — empty + harmless when the flag is off.
// The blob ACCOUNT url is shared infra: it backs both the document library and the
// inline code interpreter's EPHEMERAL original-byte retention (which writes to a
// SEPARATE container on the same account), so it is emitted when EITHER feature is
// on. The library-specific documents container is emitted only under understanding.
var documentBlobAccountEnv = ((documentUnderstandingEnabled || inlineDocumentComputeEnabled) && !empty(documentBlobAccountUrl)) ? [
  {
    name: 'AI4IA_DOCUMENT_BLOB_ACCOUNT_URL'
    value: documentBlobAccountUrl
  }
] : []
var documentBlobEnv = (documentUnderstandingEnabled && !empty(documentBlobAccountUrl)) ? [
  {
    name: 'AI4IA_DOCUMENT_BLOB_CONTAINER'
    value: documentBlobContainer
  }
] : []
var documentCuEnv = (documentUnderstandingEnabled && !empty(cuBaseUrl)) ? [
  {
    name: 'AI4IA_CU_BASE_URL'
    value: cuBaseUrl
  }
  {
    name: 'AI4IA_CU_API_VERSION'
    value: cuApiVersion
  }
] : []
var documentEnv = documentUnderstandingEnabled ? concat([
  {
    name: 'AI4IA_DOCUMENT_UNDERSTANDING_ENABLED'
    value: 'true'
  }
], documentBlobEnv, documentCuEnv) : []

// Code interpreter endpoint (base url + model) is shared by library
// compute AND the inline-attachment code interpreter, so it is emitted when EITHER
// is on (and a base url is supplied). Emitted once here to avoid duplicate env keys
// when both features are enabled; non-empty gating keeps the default-OFF posture.
var computeCiEnv = (((documentUnderstandingEnabled && documentComputeEnabled) || inlineDocumentComputeEnabled) && !empty(codeInterpreterBaseUrl)) ? [
  {
    name: 'AI4IA_CODE_INTERPRETER_BASE_URL'
    value: codeInterpreterBaseUrl
  }
  {
    name: 'AI4IA_CODE_INTERPRETER_MODEL'
    value: codeInterpreterModel
  }
] : []
var computeEnv = (documentUnderstandingEnabled && documentComputeEnabled) ? [
  {
    name: 'AI4IA_DOCUMENT_COMPUTE_ENABLED'
    value: 'true'
  }
] : []

// Inline-attachment code interpreter (default OFF). Emits its enable flag + the
// dedicated ephemeral container name only when on; the code_interpreter endpoint it
// uses comes from computeCiEnv above, and the blob account from documentBlobAccountEnv.
var inlineComputeEnv = inlineDocumentComputeEnabled ? [
  {
    name: 'AI4IA_INLINE_DOCUMENT_COMPUTE_ENABLED'
    value: 'true'
  }
  {
    name: 'AI4IA_INLINE_ATTACHMENT_BLOB_CONTAINER'
    value: inlineAttachmentBlobContainer
  }
] : []

// Image tool: the durable blob account/container are emitted only when
// image generation is enabled AND an account is provisioned; otherwise the api
// falls back to its in-memory artifact store (fine for local/dev, but ephemeral).
var imageEnv = (imageGenerationEnabled && !empty(imageBlobAccountUrl)) ? [
  {
    name: 'AI4IA_IMAGE_BLOB_ACCOUNT_URL'
    value: imageBlobAccountUrl
  }
  {
    name: 'AI4IA_IMAGE_BLOB_CONTAINER'
    value: imageBlobContainer
  }
] : []

// Video tool: same pattern as images — the durable blob account/container
// are emitted only when video generation is enabled AND an account is provisioned;
// otherwise the api falls back to its in-memory artifact store (ephemeral).
var videoEnv = (videoGenerationEnabled && !empty(videoBlobAccountUrl)) ? [
  {
    name: 'AI4IA_VIDEO_BLOB_ACCOUNT_URL'
    value: videoBlobAccountUrl
  }
  {
    name: 'AI4IA_VIDEO_BLOB_CONTAINER'
    value: videoBlobContainer
  }
] : []

// Azure AI Search endpoint, emitted only when a search service is provisioned.
// The api reaches the data plane via its managed identity (no keys); empty here
// leaves the env var unset and the feature dormant.
var searchEnv = !empty(searchEndpoint) ? [
  {
    name: 'AI4IA_SEARCH_ENDPOINT'
    value: searchEndpoint
  }
] : []

// Container-app id is computed (not read off apiApp.id) so it can be emitted as an
// env value without a self-reference cycle on the apiApp resource. apiAppName is the
// single source of truth for the resource name below.
var apiAppName = 'ca-api-${environmentName}'
var apiAppResourceId = resourceId('Microsoft.App/containerApps', apiAppName)

// Admin dashboard resource-metric panels. The container-app and Cosmos ids always
// resolve; search/postgres ids are only emitted when those resources are deployed
// (empty -> the api leaves that env var unset and the panel stays 'unavailable').
var resourceMetricsEnv = concat(
  [
    {
      name: 'AI4IA_METRICS_CONTAINER_APP_RESOURCE_ID'
      value: apiAppResourceId
    }
    {
      name: 'AI4IA_METRICS_COSMOS_RESOURCE_ID'
      value: metricsCosmosResourceId
    }
  ],
  empty(metricsSearchResourceId) ? [] : [
    {
      name: 'AI4IA_METRICS_SEARCH_RESOURCE_ID'
      value: metricsSearchResourceId
    }
  ],
  empty(metricsPostgresResourceId) ? [] : [
    {
      name: 'AI4IA_METRICS_POSTGRES_RESOURCE_ID'
      value: metricsPostgresResourceId
    }
  ]
)

// Custom tools / BYO MCP. Default OFF: nothing is emitted, so the api
// keeps the feature dormant (404). When enabled, emit the flag and — for durable
// connection secrets (12B) — the Key Vault URI; the api MI holds Secrets Officer
// on that vault. An empty URI leaves the api on its in-memory secret store.
var customToolsVaultEnv = !empty(customToolsKeyVaultUri) ? [
  {
    name: 'AI4IA_CUSTOM_TOOLS_SECRET_VAULT_URI'
    value: customToolsKeyVaultUri
  }
] : []
var customToolsEnv = customToolsEnabled ? concat([
  {
    name: 'AI4IA_CUSTOM_TOOLS_ENABLED'
    value: 'true'
  }
], customToolsVaultEnv) : []

var apiEnv = concat([
  {
    name: 'PORT'
    value: '8080'
  }
  {
    name: 'AI4IA_ENV'
    value: appEnvironment
  }
  {
    name: 'AI4IA_AUTH_PROVIDER'
    value: authProvider
  }
  {
    name: 'AI4IA_ALLOW_DEV_AUTH'
    value: string(allowDevAuth)
  }
  {
    name: 'AI4IA_MODEL_GATEWAY_URL'
    value: modelGatewayUrl
  }
  {
    name: 'AI4IA_MODEL_GATEWAY_AUTH_MODE'
    value: modelGatewayAuthMode
  }
  {
    name: 'AI4IA_SESSION_STORE'
    value: 'cosmos'
  }
  {
    name: 'AI4IA_COSMOS_ENDPOINT'
    value: cosmosEndpoint
  }
  {
    name: 'AI4IA_COSMOS_DATABASE'
    value: cosmosDatabase
  }
  {
    name: 'AZURE_CLIENT_ID'
    value: apiIdentityClientId
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appInsightsConnectionString
  }
], gatewayKeyEnv, entraEnv, memoryEnv, adminEnv, realtimeEnv, documentEnv, documentBlobAccountEnv, computeEnv, computeCiEnv, inlineComputeEnv, imageEnv, videoEnv, searchEnv, customToolsEnv, webSearchEnv, resourceMetricsEnv)

resource apiApp 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: apiAppName
  location: location
  tags: union(tags, {
    'azd-service-name': 'api'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${apiIdentityResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnvId
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: concat(gatewaySecrets, adminSecrets, webIqSecrets)
      ingress: {
        // External for v1 so the api is directly testable before the web app
        // exists. Flip to internal once web is the only public frontend.
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          identity: apiIdentityResourceId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: apiEnv
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

// Per-app metrics for the api container app. Console/system logs already stream to
// LA via the managed environment's appLogsConfiguration (container-app logs are
// env-scoped only); this adds the per-app metric signal (HTTP 5xx, replica restarts,
// CPU/memory) into the same workspace for correlation and alerting.
resource apiDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: apiApp
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

// Monitoring Reader on the api's own container app so its managed identity can read
// Azure Monitor metrics (HTTP 5xx, replica restarts, CPU/memory) for the admin
// dashboard container-app resource panel.
var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05' // Monitoring Reader
resource apiAppMonitoringRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apiApp.id, apiIdentityPrincipalId, monitoringReaderRoleId)
  scope: apiApp
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
    principalId: apiIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output apiAppName string = apiApp.name
output apiAppId string = apiApp.id
output apiUrl string = 'https://${apiApp.properties.configuration.ingress.fqdn}'
