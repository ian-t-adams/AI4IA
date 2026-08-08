// Private tool catalog: an Azure API Center that inventories the "official" MCP
// servers already fronted by the MCP APIM (infra/modules/mcpgateway.bicep) so they
// are discoverable and governable as a private organizational tool catalog. Azure
// API Center MCP-server registrations integrate with Microsoft Foundry's private
// tool catalogs, so Foundry agents can discover the same APIM-fronted MCP URLs the
// app consumes -- one governed inventory, no second auth path.
//
// The MCP inventory remains preview, but its supported ARM children are provisioned
// here so every catalog entry is repeatable and points at the governed APIM route.
//
// Provisioned only when enablePrivateToolCatalog is true. The param DEFAULT is
// false (a fresh consumer of this template provisions no API Center); this repo
// enables it in main.parameters.json.
@description('Location for the API Center. API Center is available in a subset of regions; override if the deployment region is unsupported.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Workload token (e.g. ai4ia).')
param workload string

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Per-subscription uniqueness suffix. API Center service names are globally unique across Azure, so this must be part of the name or a redeploy into a *different* subscription collides with the environment that already holds the unsuffixed name.')
param uniqueSuffix string

@description('Curated official MCP servers exposed by the shared APIM gateway.')
param servers array

@description('Shared APIM gateway base URL. Catalog deployments append /<server>/mcp.')
param gatewayBaseUrl string

var normalizedGatewayBaseUrl = endsWith(gatewayBaseUrl, '/') ? substring(gatewayBaseUrl, 0, max(length(gatewayBaseUrl) - 1, 0)) : gatewayBaseUrl

// ---------------- API Center (private tool catalog) ----------------
// Free plan. The `sku` MUST be declared explicitly: Azure defaults it to Free on
// CREATE, but a later UPDATE (any redeploy) sends a null sku and fails validation
// ("A valid Sku is required to create or update an API Catalog") unless it is set
// here. System-assigned identity is included so the catalog can later be granted
// read access from Foundry / consumers without a resource replace.
resource apiCenter 'Microsoft.ApiCenter/services@2024-06-01-preview' = {
  name: take('apic-${workload}-${environmentName}-${uniqueSuffix}', 90)
  location: location
  tags: tags
  #disable-next-line BCP187
  sku: {
    name: 'Free'
  }
}

// API Center currently supports a single, default workspace for all child assets.
resource defaultWorkspace 'Microsoft.ApiCenter/services/workspaces@2024-06-01-preview' = {
  parent: apiCenter
  name: 'default'
  properties: {
    title: 'Default workspace'
    description: 'Default workspace holding the AI4IA private MCP tool catalog.'
  }
}

resource apimEnvironment 'Microsoft.ApiCenter/services/workspaces/environments@2024-06-01-preview' = {
  parent: defaultWorkspace
  name: 'official-mcp-apim'
  properties: {
    title: 'Official MCP APIM gateway'
    description: 'Managed production environment for the APIM-fronted official MCP servers.'
    kind: 'production'
  }
}

@batchSize(1)
resource mcpApis 'Microsoft.ApiCenter/services/workspaces/apis@2024-06-01-preview' = [for server in servers: {
  parent: defaultWorkspace
  name: server.name
  properties: {
    title: server.displayName
    kind: 'mcp'
    description: empty(server.?description ?? '') ? 'Official MCP server fronted by the shared APIM gateway.' : server.description
  }
}]

@batchSize(1)
resource mcpApiVersions 'Microsoft.ApiCenter/services/workspaces/apis/versions@2024-06-01-preview' = [for (server, i) in servers: {
  parent: mcpApis[i]
  name: 'v1-preview'
  properties: {
    title: 'v1'
    lifecycleStage: 'preview'
  }
}]

@batchSize(1)
resource mcpDeployments 'Microsoft.ApiCenter/services/workspaces/apis/deployments@2024-06-01-preview' = [for (server, i) in servers: {
  parent: mcpApis[i]
  name: 'apim'
  properties: {
    title: 'APIM consumer endpoint'
    description: 'Governed Streamable HTTP endpoint exposed by the shared APIM gateway.'
    environmentId: apimEnvironment.id
    server: {
      runtimeUri: [
        '${normalizedGatewayBaseUrl}/${server.name}/mcp'
      ]
    }
    state: 'active'
  }
  dependsOn: [
    mcpApiVersions[i]
  ]
}]

@description('API Center (private tool catalog) resource name.')
output apiCenterName string = apiCenter.name

@description('API Center resource ID.')
output apiCenterId string = apiCenter.id
