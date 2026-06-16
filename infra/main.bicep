// AI4IA root deployment (subscription scope).
// Phase 0: creates the resource group + tags and wires the model catalog input.
// Phase 1+ adds modules (foundry, models, containerapps, cosmos, postgres, apim, proxy, ...).
targetScope = 'subscription'

@minLength(3)
@maxLength(20)
@description('Workload token used in resource names.')
param workload string = 'ai4ia'

@description('azd environment name (e.g. ai4ia-dev). Drives RG + tags.')
param environmentName string

@description('Primary location for the resource group and shared resources.')
param location string = 'eastus2'

@description('Accountable owner tag value.')
param owner string = 'ian-t-adams'

@description('Cost center tag value.')
param costCenter string = 'genai-demo'

@description('Monthly cost budget (billing currency) for the resource group.')
param budgetAmount int = 1500

@description('Emails notified on budget thresholds (empty = tracking only).')
param budgetAlertEmails array = ['ianadams@microsoft.com']

@description('APIM publisher email for the model gateway front door.')
param apimPublisherEmail string = 'ianadams@microsoft.com'

@description('Application runtime environment for the api (maps to AI4IA_ENV).')
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
param apiAuthProvider string = 'dev'

@description('Permit dev auth outside local (non-prod demos without Entra). Forced false in prod.')
param apiAllowDevAuth bool = true

@description('Entra tenant ID (required when apiAuthProvider == entra).')
param entraTenantId string = ''

@description('Entra audience / API app ID URI (required when apiAuthProvider == entra).')
param entraAudience string = ''

@description('Entra SPA app registration client ID for the web frontend (required to enable browser sign-in).')
param entraWebClientId string = ''

@description('API scope the web SPA requests (empty derives <entraAudience>/.default when an audience is set).')
param entraApiScope string = ''

@description('Enable Voice Live (Phase 10) end to end: the API realtime relay and the browser live-voice control. Default OFF (no behavior change).')
param voiceLiveEnabled bool = false

@description('Comma-separated browser Origin allowlist for the live-voice relay handshake (required when voiceLiveEnabled in a deployed env; the relay fails closed otherwise).')
param realtimeAllowedOrigins string = ''

@description('Enable governed tool calling inside a live voice session (calculator, current time). Inert unless voiceLiveEnabled is also true. Default OFF in bicep (matches the image/video feature pattern); set TRUE in main.parameters.json so enabling Voice Live in the live env gives the assistant tools.')
param voiceLiveToolsEnabled bool = false

@description('Enable the per-user document library (Phase 11A storage spine). Default OFF: the /api/library API refuses (404) and nothing is constructed, so there is no behavior change.')
param documentUnderstandingEnabled bool = false

@description('Content Understanding endpoint base URL (Phase 11B). Required when enabling document understanding in a deployed env; the api fails closed at startup otherwise. Empty by default (feature off).')
param cuBaseUrl string = ''

@description('Enable compute over the library (Phase 11C): intent router + code_interpreter + "adjust & return" export. Layered ON TOP of documentUnderstandingEnabled. Default OFF (the chat hot path is byte-for-byte unchanged).')
param documentComputeEnabled bool = false

@description('Azure OpenAI resource endpoint (e.g. https://<resource>.openai.azure.com) that serves the Responses API code_interpreter tool (Phase 11C). Required when enabling document compute in a deployed env; the api fails closed at startup otherwise. Empty by default (feature off).')
param codeInterpreterBaseUrl string = ''

@description('Deployment/model name that serves the Responses API code_interpreter tool (Phase 11C, e.g. gpt-4.1). Required when enabling document compute in a deployed env.')
param codeInterpreterModel string = ''

@description('Enable the agent-callable generate_image tool (Phase 11F). Default OFF. When on, a dedicated image blob storage account is provisioned and any agent may attach generate_image; produced images persist durably and serve through an authenticated endpoint.')
param imageGenerationEnabled bool = false

@description('Enable the agent-callable generate_video tool (Phase 11G, Sora 2). Default OFF. When on, a videos container is provisioned on the shared generated-media account and any agent may attach generate_video; produced clips persist durably and serve through an authenticated endpoint.')
param videoGenerationEnabled bool = false

@description('Provision an Azure AI Search service (for indexing/retrieval). Default OFF: nothing is created. When on, the api identity gets data-plane RBAC (Index Data Contributor + Service Contributor) and AI4IA_SEARCH_ENDPOINT is emitted to the api.')
param searchEnabled bool = false

@description('Azure AI Search SKU when searchEnabled (basic is the smallest tier with semantic ranking).')
param searchSku string = 'basic'

@description('Region for the Azure AI Search service. Empty => use the primary location. Provided as a separate knob because Search SKU capacity is region-constrained: eastus2 returned InsufficientResourcesAvailable, so Search is placed in a region with capacity (overridable via AI4IA_SEARCH_LOCATION). The service is reached over its global *.search.windows.net endpoint, so a different region from the rest of the stack is fine.')
param searchLocation string = ''

@description('Enable custom tools / bring-your-own MCP servers (Phase 12). Default OFF: the per-user MCP registry is never built and /api/agents/mcp-servers refuses (404), so app behavior is unchanged. When on, the api managed identity is granted Key Vault Secrets Officer (to persist per-user MCP connection secrets) and the flag + vault URI are emitted to the api.')
param customToolsEnabled bool = false

@description('Comma-separated admin subjects for the entitlement-management API.')
param adminSubjects string = ''

@description('Shared secret for the entitlement-management API under spoofable dev auth. Empty => identity-only admin (fail-closed under dev auth in a deployed env).')
@secure()
param adminApiSecret string = ''

@description('Dev user the web proxy injects as X-Dev-User (dev/demo only; ignored in prod).')
param webDevUser string = 'dev@ai4ia.local'

@description('Custom domain bound to the web app ingress (empty disables the binding).')
param webCustomDomain string = ''

@description('Existing Azure-managed cert name the web app adopts (empty derives a stable name).')
param webManagedCertName string = ''

@description('Custom domain bound to the proxy ingress (empty disables the binding).')
param proxyCustomDomain string = ''

@description('Existing Azure-managed cert name the proxy adopts (empty derives a stable name).')
param proxyManagedCertName string = ''

@description('Deploy the Postgres Flexible Server (pgvector home for mem0). Derived from postgresLocation: empty location => skip. Disable where the subscription is offer-restricted for Postgres; mem0/pgvector is a Phase 5 dependency the MVP api/web do not consume.')
param postgresLocation string = ''

var tags = {
  workload: workload
  env: environmentName
  'azd-env-name': environmentName
  costCenter: costCenter
  owner: owner
  managedBy: 'azd-bicep'
}

var resourceGroupName = 'rg-${workload}-${environmentName}'

@description('Foundry legacy naming token kept per the approved convention.')
var foundryToken = 'aiforia'

@description('Subscription token used in deployment names (region/datazone notation).')
var subscriptionToken = 'slurmfactory'

// Curated, data-driven model catalog (see infra/models.json + models.schema.json).
var models = loadJsonContent('models.json')
var skuShort = models.naming.skuShort
var catalog = models.catalog
var regionList = map(items(models.regions), r => {
  name: r.key
  dataZone: r.value.dataZone
  primary: r.value.primary
})

var uniqueSuffix = uniqueString(subscription().id, environmentName)

// Postgres (mem0/pgvector) is opt-in via a non-empty postgresLocation, since several
// subscriptions are offer-restricted for Postgres Flexible Server in some regions.
var postgresEnabled = !empty(postgresLocation)

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// --- Cross-cutting baselines (single instance, primary region) ---
module identity 'modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    location: location
    tags: tags
    environmentName: environmentName
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    location: location
    tags: tags
    workload: workload
    environmentName: environmentName
  }
}

// All app identity principals (api, web, proxy) may read secrets/config.
var allPrincipalIds = map(identity.outputs.identities, x => x.principalId)
// Only api + proxy reach the model data plane.
var dataPlanePrincipalIds = map(filter(identity.outputs.identities, x => x.service != 'web'), x => x.principalId)

// The api identity owns the canonical data stores (Cosmos + Postgres).
var apiIdentity = filter(identity.outputs.identities, x => x.service == 'api')[0]
// The proxy identity runs the SimpleL7Proxy gateway container.
var proxyIdentity = filter(identity.outputs.identities, x => x.service == 'proxy')[0]
// The web identity runs the Next.js frontend container (ACR pull only).
var webIdentity = filter(identity.outputs.identities, x => x.service == 'web')[0]

module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  scope: rg
  params: {
    location: location
    tags: tags
    workload: workload
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
    readerPrincipalIds: allPrincipalIds
    // Custom tools / BYO MCP (Phase 12B): the api MI writes per-user MCP
    // connection secrets at runtime, which needs Secrets Officer (write), not the
    // read-only Secrets User above. Granted only when the feature is enabled.
    secretsOfficerPrincipalIds: customToolsEnabled ? [apiIdentity.principalId] : []
  }
}

module data 'modules/data.bicep' = {
  name: 'data'
  scope: rg
  params: {
    location: location
    tags: tags
    workload: workload
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
    apiPrincipalId: apiIdentity.principalId
    apiPrincipalName: apiIdentity.name
    deployPostgres: postgresEnabled
    postgresLocation: empty(postgresLocation) ? location : postgresLocation
    // Document library blob storage (Phase 11B). Gated on the feature flag, so the
    // storage account + container + RBAC are created only when enabled — default OFF.
    deployDocumentStorage: documentUnderstandingEnabled
    // Generated-image blob storage (Phase 11F). Dedicated, independent of the
    // library account; gated on the image-generation flag — default OFF.
    deployImageStorage: imageGenerationEnabled
    // Generated-video container (Phase 11G) on the same shared media account;
    // gated on the video-generation flag — default OFF.
    deployVideoStorage: videoGenerationEnabled
  }
}

// Azure AI Search service (owner-requested, for indexing/retrieval). Always
// declared; resources are gated internally on searchEnabled so nothing is created
// by default. The api identity gets data-plane RBAC when provisioned.
module search 'modules/search.bicep' = {
  name: 'search'
  scope: rg
  params: {
    location: empty(searchLocation) ? location : searchLocation
    tags: tags
    workload: workload
    uniqueSuffix: uniqueSuffix
    apiPrincipalId: apiIdentity.principalId
    deploySearch: searchEnabled
    sku: searchSku
  }
}

module platform 'modules/containerapps.bicep' = {
  name: 'platform'
  scope: rg
  params: {
    location: location
    tags: tags
    workload: workload
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
    logAnalyticsName: monitoring.outputs.logAnalyticsName
    acrPullPrincipalIds: allPrincipalIds
  }
}

module eventhubs 'modules/eventhubs.bicep' = {
  name: 'eventhubs'
  scope: rg
  params: {
    location: location
    tags: tags
    workload: workload
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
    senderPrincipalIds: dataPlanePrincipalIds
    receiverPrincipalIds: [
      apiIdentity.principalId
    ]
  }
}

module cost 'modules/cost.bicep' = {
  name: 'cost'
  scope: rg
  params: {
    name: 'budget-${workload}-${environmentName}'
    amount: budgetAmount
    alertEmails: budgetAlertEmails
  }
}

// --- Phase 1.5: minimal model gateway (SimpleL7Proxy + APIM front door) ---
// APIM routes the model data plane straight to the primary Foundry account (the
// one co-located with the shared resources / `location`) until SimpleL7Proxy is
// vendored in Phase 6. The index is start-computable from the catalog; the
// Foundry outputs are referenced inline in the module params (deferred), exactly
// like `foundryEndpoints` below, so no new dependency cycle is introduced.
var regionNames = map(regionList, r => r.name)
var primaryFoundryIndex = filter(range(0, length(regionList)), i => regionNames[i] == location)[0]

// Content Understanding (Phase 11B) and the code_interpreter Responses API (Phase
// 11C) reach the Foundry data plane directly — the api identity already holds
// "Cognitive Services User" + "Cognitive Services OpenAI User" on every Foundry
// account (see foundry.bicep). When an explicit endpoint isn't supplied, default
// both to the primary Foundry account so enabling the document flags "just works"
// without hand-wiring a URL. The CI model defaults to the primary-region
// gpt-4.1-mini deployment (naming: {model}-slurmfactory-{region}-glbl).
var primaryFoundryEndpoint = foundry[primaryFoundryIndex].outputs.endpoint
var effectiveCuBaseUrl = !empty(cuBaseUrl) ? cuBaseUrl : primaryFoundryEndpoint
var effectiveCodeInterpreterBaseUrl = !empty(codeInterpreterBaseUrl) ? codeInterpreterBaseUrl : primaryFoundryEndpoint
var effectiveCodeInterpreterModel = !empty(codeInterpreterModel) ? codeInterpreterModel : 'gpt-4.1-mini-${subscriptionToken}-${location}-glbl'

module gateway 'modules/gateway.bicep' = {
  name: 'gateway'
  scope: rg
  params: {
    location: location
    tags: tags
    workload: workload
    environmentName: environmentName
    containerEnvId: platform.outputs.containerEnvId
    proxyIdentityResourceId: proxyIdentity.resourceId
    proxyIdentityClientId: proxyIdentity.clientId
    acrLoginServer: platform.outputs.acrLoginServer
    foundryEndpoints: [for (r, i) in regionList: foundry[i].outputs.endpoint]
    primaryFoundryEndpoint: foundry[primaryFoundryIndex].outputs.endpoint
    primaryFoundryAccountName: foundry[primaryFoundryIndex].outputs.accountName
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    apimPublisherEmail: apimPublisherEmail
    customDomain: proxyCustomDomain
    managedCertificateName: proxyManagedCertName
    containerEnvName: platform.outputs.containerEnvName
  }
}

// --- Phase 2: backend API (FastAPI) Container App ---
module api 'modules/api.bicep' = {
  name: 'api'
  scope: rg
  params: {
    location: location
    tags: tags
    environmentName: environmentName
    containerEnvId: platform.outputs.containerEnvId
    apiIdentityResourceId: apiIdentity.resourceId
    apiIdentityClientId: apiIdentity.clientId
    acrLoginServer: platform.outputs.acrLoginServer
    modelGatewayUrl: gateway.outputs.modelGatewayUrl
    modelGatewayAuthMode: 'api_key'
    modelGatewayApiKey: gateway.outputs.gatewaySubscriptionKey
    cosmosEndpoint: data.outputs.cosmosEndpoint
    cosmosDatabase: data.outputs.cosmosDatabaseName
    // Per-user memory: real mem0 (LLM extraction + pgvector) when Postgres is
    // deployed, else disabled. The legacy custom 'pgvector' store remains
    // available as a one-value revert (its table is untouched and coexists).
    memoryStore: postgresEnabled ? 'mem0' : 'disabled'
    postgresHost: data.outputs.postgresFqdn
    postgresDatabase: data.outputs.postgresDatabaseName
    postgresUser: apiIdentity.name
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    appEnvironment: appEnvironment
    authProvider: apiAuthProvider
    // Never allow dev auth in prod regardless of the supplied flag.
    allowDevAuth: appEnvironment == 'prod' ? false : apiAllowDevAuth
    entraTenantId: entraTenantId
    entraAudience: entraAudience
    adminSubjects: adminSubjects
    adminApiSecret: adminApiSecret
    // Voice Live (Phase 10) realtime relay. Default OFF; the Origin allowlist is
    // required (non-empty) when enabling in a deployed env or the relay fails closed.
    realtimeEnabled: voiceLiveEnabled
    realtimeAllowedOrigins: realtimeAllowedOrigins
    realtimeToolsEnabled: voiceLiveToolsEnabled
    // Document library (Phase 11A). Default OFF; the /api/library API refuses (404).
    documentUnderstandingEnabled: documentUnderstandingEnabled
    // Document ingest (Phase 11B): the blob account/container the data module
    // provisions when enabled, and the Content Understanding endpoint. Emitted to
    // the api env only when the feature is on (and CU only when a base URL is set).
    documentBlobAccountUrl: data.outputs.documentBlobAccountUrl
    documentBlobContainer: data.outputs.documentBlobContainerName
    cuBaseUrl: effectiveCuBaseUrl
    // Compute over the library (Phase 11C). Default OFF; layered on top of
    // documentUnderstandingEnabled. Base url + model emitted only when non-empty.
    documentComputeEnabled: documentComputeEnabled
    codeInterpreterBaseUrl: effectiveCodeInterpreterBaseUrl
    codeInterpreterModel: effectiveCodeInterpreterModel
    // Agent-callable image tool (Phase 11F). Default OFF; the dedicated image blob
    // account/container are emitted to the api env only when the feature is on and
    // the data module provisioned an account (else the api uses an in-memory store).
    imageGenerationEnabled: imageGenerationEnabled
    imageBlobAccountUrl: data.outputs.imageBlobAccountUrl
    imageBlobContainer: data.outputs.imageBlobContainerName
    // Agent-callable video tool (Phase 11G). Default OFF; the videos container on
    // the shared media account is emitted to the api env only when on and the data
    // module provisioned it (else the api uses an in-memory store).
    videoGenerationEnabled: videoGenerationEnabled
    videoBlobAccountUrl: data.outputs.videoBlobAccountUrl
    videoBlobContainer: data.outputs.videoBlobContainerName
    // Azure AI Search (for indexing/retrieval). The endpoint is emitted to the api
    // env only when the service is provisioned (searchEnabled); the api reaches it
    // via managed identity (no keys). Empty string when off -> env var not set.
    searchEndpoint: search.outputs.searchEndpoint
    // Custom tools / BYO MCP (Phase 12). Default OFF. When on, the flag is emitted
    // and durable MCP connection secrets are stored in the shared Key Vault (the
    // api MI holds Secrets Officer on it); only a secret reference lands in Cosmos.
    customToolsEnabled: customToolsEnabled
    customToolsKeyVaultUri: customToolsEnabled ? keyvault.outputs.keyVaultUri : ''
  }
}

// --- Phase 3: frontend web (Next.js) Container App ---
module web 'modules/web.bicep' = {
  name: 'web'
  scope: rg
  params: {
    location: location
    tags: tags
    environmentName: environmentName
    containerEnvId: platform.outputs.containerEnvId
    webIdentityResourceId: webIdentity.resourceId
    acrLoginServer: platform.outputs.acrLoginServer
    apiBaseUrl: api.outputs.apiUrl
    appEnvironment: appEnvironment
    // Dev user only matters while the api runs the dev auth provider.
    devUser: appEnvironment == 'prod' ? '' : webDevUser
    customDomain: webCustomDomain
    managedCertificateName: webManagedCertName
    containerEnvName: platform.outputs.containerEnvName
    // Browser sign-in mirrors the api: only enabled when the api enforces entra
    // and a web client id is supplied. Scope defaults to <audience>/.default.
    authProvider: apiAuthProvider
    entraClientId: entraWebClientId
    entraTenantId: entraTenantId
    entraApiScope: !empty(entraApiScope) ? entraApiScope : (empty(entraAudience) ? '' : '${entraAudience}/.default')
    // Voice Live (Phase 10): surface the flag + the API's public URL so the browser
    // can open the live-voice WebSocket directly against the API ingress (the web
    // proxy can't proxy WebSockets). Emitted only when enabled; default OFF.
    voiceLiveEnabled: voiceLiveEnabled
    apiPublicUrl: api.outputs.apiUrl
    // Document library (Phase 11B-2): surface the same feature flag that drives the
    // API ingest/retrieval path so the browser shows the library UI only when the
    // feature is on. Default OFF -> no control, no change.
    documentLibraryEnabled: documentUnderstandingEnabled
    // Custom tools / BYO remote-MCP (Phase 12B): surface the same feature flag that
    // drives the API's MCP-server CRUD + per-turn execution so the browser shows the
    // custom-tools UI only when the feature is on. Default OFF -> no control, no change.
    customToolsEnabled: customToolsEnabled
  }
}

// --- Foundry accounts + projects per region ---
// Account/project names are environment-scoped so parallel-RG validation (Phase 0b)
// and multi-env deploys don't collide on the globally-unique Cognitive Services subdomain.
// Deployment (model endpoint) names keep the region/datazone notation via subscriptionToken.
module foundry 'modules/foundry.bicep' = [for r in regionList: {
  name: 'foundry-${r.name}'
  scope: rg
  params: {
    location: r.name
    tags: tags
    accountName: take('mf-${foundryToken}-${environmentName}-${r.name}', 60)
    projectName: take('proj-default-${foundryToken}-${environmentName}-${r.name}', 60)
    dataPlanePrincipalIds: dataPlanePrincipalIds
  }
}]

// --- Curated model deployments per region (serial within each account) ---
module modelDeployments 'modules/models.bicep' = [for (r, i) in regionList: {
  name: 'models-${r.name}'
  scope: rg
  params: {
    accountName: foundry[i].outputs.accountName
    raiPolicyName: foundry[i].outputs.raiPolicyName
    deployments: flatten(map(catalog, m => map(filter(m.deployments, d => d.region == r.name), d => {
      deploymentName: '${m.name}-${subscriptionToken}-${r.name}-${skuShort[d.sku]}'
      modelName: m.name
      format: m.format
      version: d.version
      sku: d.sku
      capacity: d.capacity
    })))
  }
}]

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_LOCATION string = location
output AZURE_TAGS object = tags
output AZURE_KEY_VAULT_NAME string = keyvault.outputs.keyVaultName
output AZURE_KEY_VAULT_URI string = keyvault.outputs.keyVaultUri
output AZURE_APP_CONFIG_ENDPOINT string = keyvault.outputs.appConfigEndpoint
output AZURE_COSMOS_ENDPOINT string = data.outputs.cosmosEndpoint
output AZURE_COSMOS_DATABASE string = data.outputs.cosmosDatabaseName
output AZURE_POSTGRES_FQDN string = data.outputs.postgresFqdn
output AZURE_POSTGRES_DATABASE string = data.outputs.postgresDatabaseName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = platform.outputs.acrLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = platform.outputs.acrName
output AZURE_CONTAINER_APPS_ENVIRONMENT_NAME string = platform.outputs.containerEnvName
output AZURE_CONTAINER_APPS_ENVIRONMENT_ID string = platform.outputs.containerEnvId
output AZURE_EVENTHUBS_NAMESPACE_FQDN string = eventhubs.outputs.namespaceFqdn
output AZURE_EVENTHUBS_TELEMETRY_HUB string = eventhubs.outputs.telemetryHubName
output AZURE_MODEL_GATEWAY_URL string = gateway.outputs.modelGatewayUrl
output AZURE_APIM_GATEWAY_URL string = gateway.outputs.apimGatewayUrl
output AZURE_PROXY_URL string = gateway.outputs.proxyUrl
output AZURE_API_URL string = api.outputs.apiUrl
output AZURE_API_APP_NAME string = api.outputs.apiAppName
output AZURE_WEB_URL string = web.outputs.webUrl
output AZURE_WEB_APP_NAME string = web.outputs.webAppName
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.appInsightsConnectionString
output AZURE_FOUNDRY_ENDPOINTS array = [for (r, i) in regionList: {
  region: r.name
  dataZone: r.dataZone
  endpoint: foundry[i].outputs.endpoint
  accountName: foundry[i].outputs.accountName
}]
output AZURE_APP_IDENTITIES array = identity.outputs.identities
output AZURE_SEARCH_ENDPOINT string = search.outputs.searchEndpoint
output AZURE_SEARCH_SERVICE_NAME string = search.outputs.searchServiceName
