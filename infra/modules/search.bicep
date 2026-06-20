// Azure AI Search service for indexing/retrieval (owner-requested: "make an AI
// Search resource as we are going to ingest some items into a search index for
// ourselves"). Provisioned only when search is enabled; AAD-only (local/key auth
// disabled) so the api reaches the data plane via its managed identity + RBAC —
// no account keys, mirroring the keyless posture of Cosmos and the document
// storage account. Optional semantic ranker at the free tier.
@description('Location for the search service.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Workload token (e.g. ai4ia).')
param workload string

@description('Deterministic alphanumeric suffix for globally-unique names.')
param uniqueSuffix string

@description('Principal ID (objectId) of the api identity granted data-plane access.')
param apiPrincipalId string

@description('Provision the Azure AI Search service + RBAC. Gated so nothing is created by default — zero regression when off.')
param deploySearch bool = false

@description('Central Log Analytics workspace resource id. When the service is provisioned, diagnostic settings stream its operation logs/metrics there for the admin observability plane.')
param logAnalyticsWorkspaceId string

@description('Search service SKU. basic is the smallest tier that supports semantic ranking and is plenty for a single-tenant index.')
@allowed([
  'free'
  'basic'
  'standard'
  'standard2'
  'standard3'
])
param sku string = 'basic'

@description('Replica count (query throughput / availability). 1 is fine for a demo index.')
param replicaCount int = 1

@description('Partition count (index size / write throughput).')
param partitionCount int = 1

// Globally-unique, lowercase, 2-60 chars, alphanumeric + single dashes, no
// leading/trailing dash. uniqueString() returns a stable 13-char alphanumeric
// hash, so 'srch-ai4ia-<hash>' is well within bounds.
var searchName = toLower('srch-${workload}-${uniqueSuffix}')

// Data-plane RBAC role IDs. Index Data Contributor lets the api read/write
// documents in an index; Service Contributor lets it create/manage the index +
// indexers (needed to bootstrap an index from code).
var searchIndexDataContributorRoleId = '8ebe5a00-799e-43f5-93ac-243d3dce84a7' // Search Index Data Contributor
var searchServiceContributorRoleId = '7ca78c08-252a-4471-8644-bb5ff32d4ba0' // Search Service Contributor

resource search 'Microsoft.Search/searchServices@2023-11-01' = if (deploySearch) {
  name: searchName
  location: location
  tags: tags
  sku: {
    name: sku
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    replicaCount: replicaCount
    partitionCount: partitionCount
    hostingMode: 'default'
    publicNetworkAccess: 'enabled'
    // AAD-only: disable api keys so RBAC is the sole data-plane path.
    disableLocalAuth: true
    semanticSearch: 'free'
  }
}

resource apiSearchIndexDataRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deploySearch) {
  name: guid(search.id, apiPrincipalId, searchIndexDataContributorRoleId)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataContributorRoleId)
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource apiSearchServiceRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deploySearch) {
  name: guid(search.id, apiPrincipalId, searchServiceContributorRoleId)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchServiceContributorRoleId)
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Stream the service operation logs (query/index admin activity) + all platform
// metrics (latency, throttled/total queries) to the central Log Analytics
// workspace. Only created with the service. Retention follows the workspace.
resource searchDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (deploySearch) {
  name: 'to-log-analytics'
  scope: search
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'OperationLogs', enabled: true }
    ]
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

output searchServiceName string = deploySearch ? searchName : ''
output searchEndpoint string = deploySearch ? 'https://${searchName}.search.windows.net' : ''
