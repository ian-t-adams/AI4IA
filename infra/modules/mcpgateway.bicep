// Dedicated MCP gateway: a second APIM (Basic v2) front door that exposes and
// governs a curated set of "official" MCP servers (infra/mcp-servers.json) using
// APIM's native "expose an existing MCP server" feature. MCP traffic is gated on
// an APIM subscription key, isolated from the Consumption model gateway so the
// model data path keeps scale-to-zero economics while MCP gets a v2 tier (the
// MCP feature is NOT supported on the Consumption SKU).
//
// Default-OFF + ships empty: main.bicep deploys this module only when
// enableOfficialMcp is true, and the catalog ships with zero servers, so by
// default nothing here is provisioned.
@description('Location for the MCP gateway resources.')
param location string

@description('Tags applied to all resources.')
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

@description('SKU capacity (scale units) for the Basic v2 MCP APIM.')
param skuCapacity int = 1

@description('''Curated MCP servers to expose, loaded by main.bicep from infra/mcp-servers.json. Each item:
{ name: string, displayName: string, description?: string, upstreamUrl: string, upstreamAuthMode: 'none'|'managed_identity', upstreamMiResource?: string }''')
param servers array

// ---------------- MCP APIM front door (Basic v2) ----------------
// Basic v2 is the cheapest tier that supports the native MCP server feature
// (Consumption is excluded). System-assigned identity backs optional
// managed-identity auth to Azure-hosted upstreams.
resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' = {
  name: take('apim-mcp-${workload}-${environmentName}', 50)
  location: location
  tags: tags
  sku: {
    name: 'BasicV2'
    capacity: skuCapacity
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: apimPublisherEmail
    publisherName: apimPublisherName
  }
}

// One APIM backend per curated server, targeting the upstream Streamable-HTTP
// MCP endpoint verbatim (the catalog already records the full /mcp URL).
@batchSize(1)
resource mcpBackends 'Microsoft.ApiManagement/service/backends@2024-06-01-preview' = [for s in servers: {
  parent: apim
  name: '${s.name}-backend'
  properties: {
    protocol: 'http'
    url: s.upstreamUrl
    tls: {
      validateCertificateChain: true
      validateCertificateName: true
    }
    type: 'Single'
  }
}]

// One MCP API (type: 'mcp') per curated server. subscriptionRequired:true is the
// inbound gate — APIM rejects calls without a valid subscription key. The route
// is https://<mcp-apim>/<name>/mcp (Streamable HTTP).
@batchSize(1)
resource mcpApis 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = [for (s, i) in servers: {
  parent: apim
  name: '${s.name}-mcp'
  properties: {
    displayName: s.displayName
    description: empty(s.?description ?? '') ? '${s.displayName} MCP Server' : s.description
    type: 'mcp'
    subscriptionRequired: true
    // backendId + mcpProperties are valid for the preview MCP API type but not yet
    // in Bicep's type registry for apis@2024-06-01-preview -> BCP037. Suppressed.
    #disable-next-line BCP037
    backendId: mcpBackends[i].name
    path: '${s.name}/mcp'
    protocols: [
      'https'
    ]
    #disable-next-line BCP037
    mcpProperties: {
      transportType: 'streamable'
    }
    subscriptionKeyParameterNames: {
      header: 'Ocp-Apim-Subscription-Key'
      query: 'subscription-key'
    }
    isCurrent: true
  }
}]

// Per-server policy: route to the backend and, for managed_identity upstreams,
// inject the APIM system identity as a bearer token. The response body is never
// touched (context.Response.Body) — doing so buffers and breaks MCP streaming.
@batchSize(1)
resource mcpPolicies 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = [for (s, i) in servers: {
  parent: mcpApis[i]
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: s.upstreamAuthMode == 'managed_identity'
      ? '<policies><inbound><base /><authentication-managed-identity resource="${s.?upstreamMiResource ?? ''}" output-token-variable-name="msi-access-token" ignore-error="false" /><set-header name="Authorization" exists-action="override"><value>@("Bearer " + (string)context.Variables["msi-access-token"])</value></set-header><set-backend-service backend-id="${s.name}-backend" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'
      : '<policies><inbound><base /><set-backend-service backend-id="${s.name}-backend" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'
  }
}]

// Single all-APIs-scoped subscription whose key the backend presents as
// Ocp-Apim-Subscription-Key. This keeps the MCP gateway from being an
// unauthenticated open relay to the curated tool servers.
resource mcpSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-06-01-preview' = {
  parent: apim
  name: 'ai4ia-mcp'
  properties: {
    displayName: 'AI4IA backend MCP gateway'
    scope: '/apis'
    state: 'active'
    allowTracing: false
  }
}

// Diagnostic settings -> central Log Analytics (GatewayLogs + metrics), matching
// the model gateway so MCP request logs land in the same workspace.
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

output mcpApimName string = apim.name
output mcpGatewayBaseUrl string = apim.properties.gatewayUrl

@description('All-APIs-scoped subscription key the backend presents to the MCP gateway (Ocp-Apim-Subscription-Key).')
#disable-next-line outputs-should-not-contain-secrets
output mcpGatewaySubscriptionKey string = mcpSubscription.listSecrets().primaryKey
