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
      sku: d.sku
      capacity: d.capacity
    })))
  }
}]

// --- Phase 1 (cont.) + Phase 1.5 placeholders, added incrementally ---
// module data    'modules/data.bicep'         = { ... }   // cosmos + postgres
// module apps    'modules/containerapps.bicep' = { ... }   // ACR + Container Apps env
// module gateway 'modules/gateway.bicep'      = { ... }   // SimpleL7Proxy + APIM

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_LOCATION string = location
output AZURE_TAGS object = tags
output AZURE_KEY_VAULT_NAME string = keyvault.outputs.keyVaultName
output AZURE_KEY_VAULT_URI string = keyvault.outputs.keyVaultUri
output AZURE_APP_CONFIG_ENDPOINT string = keyvault.outputs.appConfigEndpoint
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.appInsightsConnectionString
output AZURE_FOUNDRY_ENDPOINTS array = [for (r, i) in regionList: {
  region: r.name
  dataZone: r.dataZone
  endpoint: foundry[i].outputs.endpoint
  accountName: foundry[i].outputs.accountName
}]
output AZURE_APP_IDENTITIES array = identity.outputs.identities
