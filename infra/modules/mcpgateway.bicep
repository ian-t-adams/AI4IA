// Official MCP children on the shared active APIM. This module is conditional,
// but the underlying Basic v2 service is created/adopted unconditionally by
// infra/modules/apimcore.bicep so model/realtime and MCP share one gateway.
@description('Name of the shared active APIM service that owns the MCP children.')
param apimName string

@description('Gateway base URL of the shared active APIM service.')
param gatewayBaseUrl string

@description('Curated MCP servers to expose, loaded by main.bicep from infra/mcp-servers.json.')
param servers array

resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: apimName
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
// is https://<shared-apim>/<name>/mcp (Streamable HTTP).
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

// Per-server inbound policy fragments, precomputed so the policy resource stays
// readable. Each server's inbound = (optional managed-identity bearer) + (optional
// static upstream headers) + (optional static query params) + route to the backend.
// items()/map()/join()/concat() resolve at compile time because `servers` originates
// from loadJsonContent() in main.bicep. The response body is never touched
// (context.Response.Body) — buffering it breaks MCP streaming.
var serverInboundPolicies = [for s in servers: join(concat(
  s.upstreamAuthMode == 'managed_identity' ? [
    '<authentication-managed-identity resource="${s.?upstreamMiResource ?? ''}" output-token-variable-name="msi-access-token" ignore-error="false" />'
    '<set-header name="Authorization" exists-action="override"><value>@("Bearer " + (string)context.Variables["msi-access-token"])</value></set-header>'
  ] : [],
  map(items(s.?upstreamHeaders ?? {}), h => '<set-header name="${h.key}" exists-action="override"><value>${h.value}</value></set-header>'),
  map(items(s.?upstreamQueryParams ?? {}), q => '<set-query-parameter name="${q.key}" exists-action="override"><value>${q.value}</value></set-query-parameter>'),
  [
    '<set-backend-service backend-id="${s.name}-backend" />'
  ]
), '')]

@batchSize(1)
resource mcpPolicies 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = [for (s, i) in servers: {
  parent: mcpApis[i]
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: '<policies><inbound><base />${serverInboundPolicies[i]}</inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'
  }
}]

// The unconditional APIM core owns the product and narrows the legacy key even
// when official MCP is disabled. This module only associates enabled MCP APIs.
resource mcpProduct 'Microsoft.ApiManagement/service/products@2024-06-01-preview' existing = {
  parent: apim
  name: 'ai4ia-mcp'
}

resource mcpProductApis 'Microsoft.ApiManagement/service/products/apis@2024-06-01-preview' = [for (server, i) in servers: {
  parent: mcpProduct
  name: mcpApis[i].name
  dependsOn: [
    mcpApis[i]
  ]
}]

output mcpApimName string = apim.name
output mcpGatewayBaseUrl string = gatewayBaseUrl
