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

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// --- Phase 1 module wiring (placeholders, added incrementally) ---
// module monitoring 'modules/monitoring.bicep' = { ... }
// module identity   'modules/identity.bicep'   = { ... }
// module foundry    'modules/foundry.bicep'    = { ... }
// module models     'modules/models.bicep'     = { ... }   // iterates infra/models.json
// module data       'modules/data.bicep'       = { ... }   // cosmos + postgres
// module apps       'modules/containerapps.bicep' = { ... }
// module gateway    'modules/gateway.bicep'    = { ... }   // SimpleL7Proxy + APIM

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_LOCATION string = location
output AZURE_TAGS object = tags
