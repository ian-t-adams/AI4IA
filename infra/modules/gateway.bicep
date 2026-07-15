// Governed model data plane:
// HTTP/SSE callers -> SimpleL7Proxy -> APIM -> catalog-driven Foundry backends.
// Realtime WebSockets deliberately bypass the proxy through a separately scoped
// APIM API because SimpleL7Proxy is an HTTP/SSE worker, not a WebSocket proxy.
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

@description('Client ID of the proxy user-assigned identity.')
param proxyIdentityClientId string

@description('ACR login server the proxy image is pulled from.')
param acrLoginServer string

@description('Container image for the proxy; azd replaces the default with the built /proxy image.')
param proxyImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Catalog-driven Foundry backends: region, endpoint, and accountName.')
param foundryBackends array

@description('Foundry endpoint in the deployment primary region, used only as the non-looping APIM service URL fallback.')
param primaryFoundryEndpoint string

@description('Application Insights connection string for proxy telemetry.')
param appInsightsConnectionString string

@description('App Configuration endpoint used for managed-identity hot reload.')
param appConfigEndpoint string = ''

@description('Optional App Configuration label for this proxy deployment.')
param appConfigLabel string = ''

@minValue(10)
@description('Warm-setting refresh interval in seconds.')
param appConfigRefreshIntervalSeconds int = 30

@description('Enable metadata-only proxy Event Hub telemetry. Default OFF.')
param proxyEventHubTelemetryEnabled bool = false

@description('Event Hubs namespace FQDN used with managed identity.')
param eventHubNamespaceFqdn string = ''

@description('Event Hub name used for proxy telemetry.')
param eventHubName string = ''

@description('Enable the server-owned multi-application profile snapshot. Default OFF.')
param proxyProfilesEnabled bool = false

@secure()
@description('Minimal profile projection JSON. Mounted as an ACA secret file only when profiles are enabled.')
param proxyProfileProjectionJson string = ''

@description('Enable priority-key mapping and reserved workers. Default OFF.')
param proxyPrioritiesEnabled bool = false

@description('Reserved workers in priority:count format. Used only when priorities are enabled.')
param proxyPriorityWorkers string = ''

@minValue(1)
@description('SimpleL7Proxy worker count per replica.')
param proxyWorkers int = 10

@minValue(1)
@description('Keep at least one proxy replica warm because the synchronous queue is in-memory per replica.')
param proxyMinReplicas int = 1

@minValue(1)
@description('Maximum proxy replicas.')
param proxyMaxReplicas int = 3

@description('Enable durable async Blob + Service Bus integration. Default OFF.')
param proxyAsyncEnabled bool = false

@description('Managed-identity Blob service URI for durable async results.')
param proxyAsyncBlobUri string = ''

@description('Managed-identity Service Bus namespace FQDN for durable async requests.')
param proxyAsyncServiceBusNamespace string = ''

@description('Service Bus queue used by durable async mode.')
param proxyAsyncServiceBusQueue string = 'requeststatus'

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

var foundryBase = endsWith(primaryFoundryEndpoint, '/') ? primaryFoundryEndpoint : '${primaryFoundryEndpoint}/'
var foundryOpenAiUrl = '${foundryBase}openai'
var proxyAppName = 'ca-proxy-${environmentName}'

// ---------------- APIM trust boundary ----------------
resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
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

resource foundryEndpointValues 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = [for backend in foundryBackends: {
  parent: apim
  name: 'foundry-${backend.region}-endpoint'
  properties: {
    displayName: 'foundry-${backend.region}-endpoint'
    secret: false
    value: endsWith(backend.endpoint, '/') ? substring(backend.endpoint, 0, length(backend.endpoint) - 1) : backend.endpoint
  }
}]

var endpointSelectionFragmentDefinitions = [
  {
    name: 'endpoint_selection_catalog_0_31'
    description: 'Generated model/deployment catalog chunk 0 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-0.xml')
  }
  {
    name: 'endpoint_selection_catalog_1_31'
    description: 'Generated model/deployment catalog chunk 1 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-1.xml')
  }
  {
    name: 'endpoint_selection_catalog_2_31'
    description: 'Generated model/deployment catalog chunk 2 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-2.xml')
  }
  {
    name: 'endpoint_selection_catalog_3_31'
    description: 'Generated model/deployment catalog chunk 3 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-3.xml')
  }
  {
    name: 'endpoint_selection_setup_31'
    description: 'Generated model routing setup for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints.xml')
  }
]

resource endpointSelectionFragments 'Microsoft.ApiManagement/service/policyFragments@2024-05-01' = [for definition in endpointSelectionFragmentDefinitions: {
  parent: apim
  name: definition.name
  properties: {
    description: definition.description
    format: 'rawxml'
    value: definition.value
  }
  dependsOn: [
    foundryEndpointValues
  ]
}]

resource modelsApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'openai'
  properties: {
    displayName: 'SimpleL7Proxy model backend'
    path: 'openai'
    protocols: [
      'https'
    ]
    // The policy always selects a catalog backend. This non-proxy fallback keeps
    // the topology acyclic even if a policy is removed during troubleshooting.
    serviceUrl: foundryOpenAiUrl
    subscriptionRequired: true
    apiType: 'http'
  }
}

var modelMethods = [
  'POST'
  'GET'
  'PUT'
  'PATCH'
  'DELETE'
]

resource modelOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [for method in modelMethods: {
  parent: modelsApi
  name: 'proxy-${toLower(method)}'
  properties: {
    displayName: 'Proxy ${method}'
    method: method
    urlTemplate: '/{*path}'
    templateParameters: [
      {
        name: 'path'
        type: 'string'
        required: true
      }
    ]
  }
}]

resource modelsApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: modelsApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/simplel7proxy-priority-retry.xml')
  }
  dependsOn: [
    endpointSelectionFragments
  ]
}

// Only the proxy receives this subscription. It is injected from an ACA secret
// into Host1-api-key and never returned to application callers.
resource proxyModelSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: 'ai4ia-proxy-models'
  properties: {
    displayName: 'AI4IA SimpleL7Proxy model hop'
    scope: modelsApi.id
    state: 'active'
    allowTracing: false
  }
}

// The realtime API is intentionally separate and more specific than /openai.
// Its key cannot invoke the normal model API, preventing callers from bypassing
// the proxy for compatible HTTP/SSE traffic.
resource realtimeApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'openai-realtime'
  properties: {
    displayName: 'FastAPI realtime relay backend'
    path: 'openai/realtime'
    protocols: [
      'https'
    ]
    serviceUrl: '${foundryOpenAiUrl}/realtime'
    subscriptionRequired: true
    apiType: 'http'
  }
}

resource realtimeOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: realtimeApi
  name: 'realtime-connect'
  properties: {
    displayName: 'Realtime WebSocket connect'
    method: 'GET'
    urlTemplate: '/'
  }
}

resource realtimeApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: realtimeApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/realtime-routing.xml')
  }
  dependsOn: [
    foundryEndpointValues
  ]
}

resource apiRealtimeSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: 'ai4ia-api-realtime'
  properties: {
    displayName: 'AI4IA FastAPI realtime relay'
    scope: realtimeApi.id
    state: 'active'
    allowTracing: false
  }
}

// APIM is the only identity granted normal model access by this module.
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var cognitiveUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource foundryAccounts 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = [for backend in foundryBackends: {
  name: backend.accountName
}]

resource apimOpenAiUsers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (backend, i) in foundryBackends: {
  name: guid(foundryAccounts[i].id, apim.id, openAiUserRoleId)
  scope: foundryAccounts[i]
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
  }
}]

resource apimCognitiveUsers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (backend, i) in foundryBackends: {
  name: guid(foundryAccounts[i].id, apim.id, cognitiveUserRoleId)
  scope: foundryAccounts[i]
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveUserRoleId)
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
  }
}]

// ---------------- SimpleL7Proxy Container App ----------------
var hostEnv = [
  {
    name: 'Host1'
    value: 'host=${apim.properties.gatewayUrl};mode=apim;probe=/openai/status;processor=OpenAI;api-key-header=Ocp-Apim-Subscription-Key;retryafter=true'
  }
  {
    name: 'Host1-api-key'
    secretRef: 'proxy-apim-subscription-key'
  }
]

var staticEnv = [
  { name: 'Port', value: '8080' }
  { name: 'Workers', value: string(proxyWorkers) }
  { name: 'MaxAttempts', value: '1' }
  { name: 'PriorityWorker', value: proxyPrioritiesEnabled ? proxyPriorityWorkers : '' }
  { name: 'DefaultPriority', value: '2' }
  { name: 'ValidateAuthConfig', value: 'enabled=true;mode=key;header=Ocp-Apim-Subscription-Key' }
  { name: 'ValidateAuthKey1', secretRef: 'api-proxy-inbound-key' }
  {
    name: 'StripRequestHeaders'
    value: string([
      'Authorization'
      'Ocp-Apim-Subscription-Key'
      'X-AI4IA-App-Id'
      'X-AI4IA-User-Id'
      'X-UserProfile'
    ])
  }
  {
    name: 'StripResponseHeaders'
    value: string([
      'backendLog'
      'X-Policy-LastError'
    ])
  }
  { name: 'LogAllRequestHeaders', value: 'false' }
  { name: 'LogAllResponseHeaders', value: 'false' }
  { name: 'LogHeaders', value: '[]' }
  { name: 'AppInsightsConnectionString', value: appInsightsConnectionString }
  { name: 'AZURE_CLIENT_ID', value: proxyIdentityClientId }
  { name: 'CONTAINER_APP_NAME', value: proxyAppName }
]

var appConfigEnv = empty(appConfigEndpoint) ? [] : concat([
  { name: 'AZURE_APPCONFIG_ENDPOINT', value: appConfigEndpoint }
  { name: 'AZURE_APPCONFIG_REFRESH_INTERVAL_SECONDS', value: string(appConfigRefreshIntervalSeconds) }
], empty(appConfigLabel) ? [] : [
  { name: 'AZURE_APPCONFIG_LABEL', value: appConfigLabel }
])

var priorityEnv = proxyPrioritiesEnabled ? [
  { name: 'PriorityKeys', value: string(['high', 'standard', 'batch']) }
  { name: 'PriorityValues', value: string([1, 2, 3]) }
] : []

var eventHubEnv = proxyEventHubTelemetryEnabled ? [
  { name: 'EVENTHUB_NAMESPACE', value: eventHubNamespaceFqdn }
  { name: 'EVENTHUB_NAME', value: eventHubName }
  { name: 'EVENT_LOGGERS', value: 'eventhub' }
] : []

var profileEnv = proxyProfilesEnabled ? [
  { name: 'UseProfiles', value: 'true' }
  { name: 'UserConfigRequired', value: 'true' }
  { name: 'UserConfigUrl', value: 'file:/mnt/ai4ia-profiles/profiles.json' }
  { name: 'UserProfileHeader', value: 'X-AI4IA-App-Id' }
  { name: 'UserIDFieldName', value: 'appId' }
] : [
  { name: 'UseProfiles', value: 'false' }
]

var asyncEnv = proxyAsyncEnabled ? [
  { name: 'AsyncModeEnabled', value: 'true' }
  { name: 'AsyncBlobStorageConfig', value: 'uri=${proxyAsyncBlobUri},mi=true' }
  { name: 'AsyncSBConfig', value: 'ns=${proxyAsyncServiceBusNamespace},q=${proxyAsyncServiceBusQueue},mi=true' }
  { name: 'StorageDbContainerName', value: 'requests' }
] : [
  { name: 'AsyncModeEnabled', value: 'false' }
]

var proxySecrets = concat([
  {
    name: 'proxy-apim-subscription-key'
    value: proxyModelSubscription.listSecrets().primaryKey
  }
  {
    name: 'api-proxy-inbound-key'
    value: apiRealtimeSubscription.listSecrets().primaryKey
  }
], proxyProfilesEnabled ? [
  {
    name: 'profile-projection-json'
    value: proxyProfileProjectionJson
  }
] : [])

var profileVolumes = proxyProfilesEnabled ? [
  {
    name: 'profile-projection'
    storageType: 'Secret'
    secrets: [
      {
        secretRef: 'profile-projection-json'
        path: 'profiles.json'
      }
    ]
  }
] : []

var profileVolumeMounts = proxyProfilesEnabled ? [
  {
    volumeName: 'profile-projection'
    mountPath: '/mnt/ai4ia-profiles'
  }
] : []

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
  name: proxyAppName
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
      secrets: proxySecrets
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
      volumes: profileVolumes
      containers: [
        {
          name: 'proxy'
          image: proxyImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(hostEnv, staticEnv, appConfigEnv, priorityEnv, eventHubEnv, profileEnv, asyncEnv)
          volumeMounts: profileVolumeMounts
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/startup'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 2
              periodSeconds: 5
              timeoutSeconds: 3
              failureThreshold: 30
              successThreshold: 1
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/liveness'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              timeoutSeconds: 3
              failureThreshold: 3
              successThreshold: 1
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/readiness'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
              successThreshold: 1
            }
          ]
        }
      ]
      scale: {
        minReplicas: proxyMinReplicas
        maxReplicas: proxyMaxReplicas
      }
    }
  }
}

// ---------------- Observability ----------------
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

var proxyUrl = 'https://${proxyApp.properties.configuration.ingress.fqdn}'

output proxyAppName string = proxyApp.name
output proxyUrl string = proxyUrl
output apimGatewayUrl string = apim.properties.gatewayUrl
output modelGatewayUrl string = '${proxyUrl}/openai'
output realtimeGatewayUrl string = '${apim.properties.gatewayUrl}/openai'

@description('API-scoped key used by FastAPI for proxy ingress and the APIM realtime API.')
@secure()
output apiGatewayKey string = apiRealtimeSubscription.listSecrets().primaryKey
