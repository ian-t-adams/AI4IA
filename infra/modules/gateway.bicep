// Model gateway: SimpleL7Proxy (Container App) behind APIM.
// The backend always calls models through this single front door so model wiring
// isn't duplicated; capacity-sharing/entitlements/quotas layer on above it.
@description('Location for the gateway resources.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Workload token (e.g. ai4ia).')
param workload string

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Container Apps managed environment resource ID.')
param containerEnvId string

@description('Central Log Analytics workspace resource ID for diagnostic settings.')
param logAnalyticsWorkspaceId string

@description('Resource ID of the proxy user-assigned identity.')
param proxyIdentityResourceId string

@description('Client ID of the proxy user-assigned identity (selects the MI for DefaultAzureCredential token requests).')
param proxyIdentityClientId string

@description('ACR login server the proxy image is pulled from.')
param acrLoginServer string

@description('Container image for the proxy; azd replaces the default with the built /proxy image.')
param proxyImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Foundry account endpoints the proxy routes model traffic to.')
param foundryEndpoints array

@description('Primary Foundry account endpoint APIM routes model traffic to.')
param primaryFoundryEndpoint string

@description('Primary Foundry account name (role-assignment scope for the APIM managed identity).')
param primaryFoundryAccountName string

@description('Application Insights connection string for proxy telemetry.')
param appInsightsConnectionString string

@description('APIM publisher email (required by APIM).')
param apimPublisherEmail string

@description('APIM publisher org name.')
param apimPublisherName string = 'AI4IA'

@description('Custom domain bound to the proxy ingress (empty disables custom-domain binding).')
param customDomain string = ''

@description('Existing Azure-managed certificate name to adopt (empty derives a stable name).')
param managedCertificateName string = ''

@description('Container Apps managed environment name (parent of the managed certificate).')
param containerEnvName string

// SimpleL7Proxy HostN connection strings (managed-identity auth to Cognitive Services).
var hostEnv = [for (e, i) in foundryEndpoints: {
  name: 'Host${i + 1}'
  value: 'host=${e};usemi=true;audience=https://cognitiveservices.azure.com;mode=direct'
}]

var staticEnv = [
  {
    name: 'Port'
    value: '8080'
  }
  {
    name: 'Workers'
    value: '10'
  }
  {
    name: 'AppInsightsConnectionString'
    value: appInsightsConnectionString
  }
  {
    // SimpleL7Proxy uses DefaultAzureCredential; with a user-assigned identity
    // it must be told which client ID to use, or token requests fail with
    // "Unable to load the proper Managed Identity".
    name: 'AZURE_CLIENT_ID'
    value: proxyIdentityClientId
  }
]

// Custom-domain binding (durable in IaC; see web.bicep for rationale).
var proxyManagedCertName = !empty(managedCertificateName) ? managedCertificateName : 'mc-${replace(customDomain, '.', '-')}'

resource managedEnv 'Microsoft.App/managedEnvironments@2024-10-02-preview' existing = {
  name: containerEnvName
}

resource proxyCert 'Microsoft.App/managedEnvironments/managedCertificates@2024-10-02-preview' = if (!empty(customDomain)) {
  parent: managedEnv
  name: proxyManagedCertName
  location: location
  properties: {
    subjectName: customDomain
    domainControlValidation: 'CNAME'
  }
}

var proxyCustomDomains = empty(customDomain) ? [] : [
  {
    name: customDomain
    bindingType: 'SniEnabled'
    certificateId: proxyCert.id
  }
]

resource proxyApp 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-proxy-${environmentName}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'proxy'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${proxyIdentityResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnvId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        customDomains: proxyCustomDomains
      }
      registries: [
        {
          server: acrLoginServer
          identity: proxyIdentityResourceId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'proxy'
          image: proxyImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(hostEnv, staticEnv)
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 3
      }
    }
  }
}

var proxyUrl = 'https://${proxyApp.properties.configuration.ingress.fqdn}'

// APIM routes model traffic to the primary Foundry account's Azure OpenAI data
// plane. The endpoint output always carries a trailing slash, but guard anyway
// so the `/openai` suffix is never doubled or fused.
var foundryBase = endsWith(primaryFoundryEndpoint, '/') ? primaryFoundryEndpoint : '${primaryFoundryEndpoint}/'
var foundryServiceUrl = '${foundryBase}openai'

// ---------------- APIM front door (Consumption) ----------------
resource apim 'Microsoft.ApiManagement/service@2023-05-01-preview' = {
  name: take('apim-${workload}-${environmentName}', 50)
  location: location
  tags: tags
  sku: {
    name: 'Consumption'
    capacity: 0
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: apimPublisherEmail
    publisherName: apimPublisherName
  }
}

resource modelsApi 'Microsoft.ApiManagement/service/apis@2023-05-01-preview' = {
  parent: apim
  name: 'openai'
  properties: {
    displayName: 'Model Gateway'
    path: 'openai'
    protocols: [
      'https'
    ]
    serviceUrl: foundryServiceUrl
    subscriptionRequired: true
    apiType: 'http'
  }
}

resource modelsApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2023-05-01-preview' = {
  parent: modelsApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: '<policies><inbound><base /><set-header name="x-correlation-id" exists-action="skip"><value>@(context.RequestId.ToString())</value></set-header><authentication-managed-identity resource="https://cognitiveservices.azure.com" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'
  }
}

resource proxyOperation 'Microsoft.ApiManagement/service/apis/operations@2023-05-01-preview' = {
  parent: modelsApi
  name: 'proxy-post'
  properties: {
    displayName: 'Proxy POST'
    method: 'POST'
    urlTemplate: '/{*path}'
    templateParameters: [
      {
        name: 'path'
        type: 'string'
        required: true
      }
    ]
  }
}

// Single API-scoped subscription whose key the backend presents as
// Ocp-Apim-Subscription-Key. This keeps the gateway from being an unauthenticated
// open relay to real models.
resource gatewaySubscription 'Microsoft.ApiManagement/service/subscriptions@2023-05-01-preview' = {
  parent: apim
  name: 'ai4ia-gateway'
  properties: {
    displayName: 'AI4IA backend gateway'
    scope: modelsApi.id
    state: 'active'
    allowTracing: false
  }
}

// APIM's system identity authenticates to the Foundry data plane via the policy
// above; grant it the same data-plane roles the app/proxy identities hold.
resource primaryFoundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: primaryFoundryAccountName
}

var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
var cognitiveUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908' // Cognitive Services User

resource apimOpenAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(primaryFoundry.id, apim.id, openAiUserRoleId)
  scope: primaryFoundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource apimCognitiveUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(primaryFoundry.id, apim.id, cognitiveUserRoleId)
  scope: primaryFoundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveUserRoleId)
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------- Observability (diagnostic settings -> central Log Analytics) ----------------
// APIM is the model front door, so its gateway request log + metrics are the
// operational proof for everything routed through the gateway. Consumption SKU
// supports GatewayLogs; LLM token logs are a separate workstream (not enabled here).
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

// Per-app metrics for the proxy (the gateway's compute data path). Console/system
// logs already stream to LA via the managed environment's appLogsConfiguration;
// container-app logs are env-scoped only, so this adds the per-app metric signal
// (5xx rate, replica restarts, CPU/memory) into the same workspace for correlation.
resource proxyDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: proxyApp
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

output proxyAppName string = proxyApp.name
output proxyUrl string = proxyUrl
output apimName string = apim.name
output apimGatewayUrl string = apim.properties.gatewayUrl
output modelGatewayUrl string = '${apim.properties.gatewayUrl}/openai'

@description('API-scoped subscription key the backend presents to the gateway (api_key auth mode).')
#disable-next-line outputs-should-not-contain-secrets
output gatewaySubscriptionKey string = gatewaySubscription.listSecrets().primaryKey
