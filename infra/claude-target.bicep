targetScope = 'resourceGroup'

import { deploymentTarget } from './model-targets.bicep'

@description('Separate operator-approved account/model provisioning. Default OFF. This is never called by main.bicep or the deploy workflow.')
param provisionClaude bool = false

@allowed(['public-keyless'])
@description('Explicit operator network choice; no default. This source unit does not implement Private Link. An unresolved/private requirement must not select public-keyless.')
param networkMode string

@minLength(3)
@maxLength(20)
param environmentName string

@minLength(3)
@description('Approved legal entity accepting Anthropic Marketplace terms in THIS subscription.')
param claudeOrganizationName string

@minLength(2)
@maxLength(2)
param claudeCountryCode string

@minLength(2)
param claudeIndustry string

var models = loadJsonContent('models.json')
var external = filter(models.catalog, model => deploymentTarget(model) == 'external-claude')
var deploymentRows = flatten(map(external, model => map(model.deployments, deployment => {
  deploymentName: '${model.name}-${models.naming.subscriptionToken}-${deployment.region}-${models.naming.skuShort[deployment.sku]}'
  modelName: model.name
  format: model.format
  version: deployment.version
  sku: deployment.sku
  capacity: deployment.capacity
})))
var accountName = 'mf-claude-${environmentName}-${uniqueString(subscription().id, resourceGroup().id, environmentName)}'
var location = external[0].deployments[0].region

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = if (provisionClaude) {
  name: accountName
  location: location
  tags: {
    'azd-env-name': environmentName
    'ai4ia-target': 'external-claude'
  }
  kind: 'AIServices'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: networkMode == 'public-keyless' ? 'Enabled' : 'Disabled'
    allowProjectManagement: false
  }
}

module deployments 'modules/models.bicep' = if (provisionClaude) {
  name: 'claude-models'
  params: {
    accountName: account.name
    deployments: deploymentRows
    claudeOrganizationName: claudeOrganizationName
    claudeCountryCode: claudeCountryCode
    claudeIndustry: claudeIndustry
  }
}

output plannedAccountName string = accountName
output expectedDeployments array = deploymentRows
output targetEndpoint string = 'https://${accountName}.services.ai.azure.com'
