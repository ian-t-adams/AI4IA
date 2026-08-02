// Shared active APIM plane: owns the apim-mcp-* Basic v2 service so
// model/realtime and optional official-MCP children attach to one gateway.
@description('Location for the shared active APIM service.')
param location string

@description('Tags applied to the shared active APIM service.')
param tags object

@description('Workload token (e.g. ai4ia).')
param workload string

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Central Log Analytics workspace resource ID for diagnostic settings.')
param logAnalyticsWorkspaceId string

@description('APIM publisher email (required by APIM).')
param apimPublisherEmail string

@description('APIM publisher org name.')
param apimPublisherName string = 'AI4IA'

@description('Per-subscription uniqueness suffix. APIM service names are globally unique across Azure (they back <name>.azure-api.net), so this must be part of the name or a redeploy into a *different* subscription fails with ServiceAlreadyExists against the environment that already holds the unsuffixed name.')
param uniqueSuffix string

// APIM child entities live in one flat namespace per service. This plane is shared
// across workloads, so every child name must carry the workload token or a second
// workload silently collides with (and overwrites) this one's product/subscription.
// Derived, not hardcoded: for workload 'ai4ia' this still emits 'ai4ia-mcp', so
// existing deployments are unchanged and no subscription key is rotated.
var mcpProductName = '${workload}-mcp'

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: take('apim-mcp-${workload}-${environmentName}-${uniqueSuffix}', 50)
  location: location
  tags: tags
  sku: {
    name: 'BasicV2'
    capacity: 1
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: apimPublisherEmail
    publisherName: apimPublisherName
  }
}

resource apimDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: apim
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'GatewayLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

// Always narrow the pre-existing MCP credential before model/realtime APIs are
// attached to this shared service. This must not depend on enableOfficialMcp:
// otherwise an older all-APIs subscription could authorize the new model APIs.
resource mcpProduct 'Microsoft.ApiManagement/service/products@2024-05-01' = {
  parent: apim
  name: mcpProductName
  properties: {
    displayName: 'AI4IA official MCP'
    description: 'Curated official MCP APIs only.'
    subscriptionRequired: true
    approvalRequired: false
    state: 'published'
  }
}

resource mcpSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: mcpProductName
  properties: {
    displayName: 'AI4IA backend MCP gateway'
    scope: '/products/${mcpProductName}'
    state: 'active'
    allowTracing: false
  }
  dependsOn: [
    mcpProduct
  ]
}

output apimName string = apim.name
output apimId string = apim.id
output gatewayUrl string = apim.properties.gatewayUrl
// Exported so mcpgateway.bicep binds to this exact product instead of re-deriving
// the name, keeping one source of truth for the shared plane's child namespace.
output mcpProductName string = mcpProductName
output principalId string = apim.identity.principalId

@description('Product-scoped subscription key for the optional official MCP APIs.')
#disable-next-line outputs-should-not-contain-secrets
output mcpSubscriptionKey string = mcpSubscription.listSecrets().primaryKey
