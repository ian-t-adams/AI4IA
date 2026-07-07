// Private tool catalog: an Azure API Center that inventories the "official" MCP
// servers already fronted by the MCP APIM (infra/modules/mcpgateway.bicep) so they
// are discoverable and governable as a private organizational tool catalog. Azure
// API Center MCP-server registrations integrate with Microsoft Foundry's private
// tool catalogs, so Foundry agents can discover the same APIM-fronted MCP URLs the
// app consumes -- one governed inventory, no second auth path.
//
// This module provisions ONLY the catalog container (the API Center service + its
// single default workspace). Registering each MCP server as an asset is a preview,
// portal/CLI/script-driven step (scripts/provision-private-tool-catalog.py), so the
// per-server registration is intentionally NOT baked into IaC here.
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

// ---------------- API Center (private tool catalog) ----------------
// Free plan. The `sku` MUST be declared explicitly: Azure defaults it to Free on
// CREATE, but a later UPDATE (any redeploy) sends a null sku and fails validation
// ("A valid Sku is required to create or update an API Catalog") unless it is set
// here. System-assigned identity is included so the catalog can later be granted
// read access from Foundry / consumers without a resource replace.
resource apiCenter 'Microsoft.ApiCenter/services@2024-03-01' = {
  name: take('apic-${workload}-${environmentName}', 90)
  location: location
  tags: tags
  sku: {
    name: 'Free'
  }
  identity: {
    type: 'SystemAssigned'
  }
}

// API Center currently supports a single, default workspace for all child assets.
resource defaultWorkspace 'Microsoft.ApiCenter/services/workspaces@2024-03-01' = {
  parent: apiCenter
  name: 'default'
  properties: {
    title: 'Default workspace'
    description: 'Default workspace holding the AI4IA private MCP tool catalog.'
  }
}

@description('API Center (private tool catalog) resource name.')
output apiCenterName string = apiCenter.name

@description('API Center resource ID.')
output apiCenterId string = apiCenter.id

@description('API Center system-assigned identity principal ID (for future catalog read grants).')
output apiCenterPrincipalId string = apiCenter.identity.principalId
