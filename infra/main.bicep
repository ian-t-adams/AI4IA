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

@description('Dev user the web proxy injects as X-Dev-User (dev/demo only; ignored in prod).')
param webDevUser string = 'dev@ai4ia.local'

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
    acrLoginServer: platform.outputs.acrLoginServer
    foundryEndpoints: [for (r, i) in regionList: foundry[i].outputs.endpoint]
    primaryFoundryEndpoint: foundry[primaryFoundryIndex].outputs.endpoint
    primaryFoundryAccountName: foundry[primaryFoundryIndex].outputs.accountName
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    apimPublisherEmail: apimPublisherEmail
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
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    appEnvironment: appEnvironment
    authProvider: apiAuthProvider
    // Never allow dev auth in prod regardless of the supplied flag.
    allowDevAuth: appEnvironment == 'prod' ? false : apiAllowDevAuth
    entraTenantId: entraTenantId
    entraAudience: entraAudience
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
