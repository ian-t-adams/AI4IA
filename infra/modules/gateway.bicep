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

@description('Name of the shared active Basic v2 APIM service.')
param sharedApimName string

@description('Resource ID of the shared active Basic v2 APIM service.')
param sharedApimResourceId string

@description('Gateway base URL of the shared active Basic v2 APIM service.')
param sharedApimGatewayUrl string

@description('System-assigned managed identity principalId of the shared active Basic v2 APIM service.')
param sharedApimPrincipalId string

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
var primaryFoundryRealtimeWssUrl = '${replace(endsWith(primaryFoundryEndpoint, '/') ? substring(primaryFoundryEndpoint, 0, length(primaryFoundryEndpoint) - 1) : primaryFoundryEndpoint, 'https://', 'wss://')}/openai/realtime'
var proxyAppName = 'ca-proxy-${environmentName}'

// ---------------- APIM trust boundary ----------------
resource sharedApim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: sharedApimName
}

resource legacyConsumptionApim 'Microsoft.ApiManagement/service@2024-05-01' = {
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

// The shared active Basic v2 APIM is adopted by apimcore.bicep and referenced
// here only as an existing parent. The legacy Consumption APIM below stays
// intact as an inactive rollback plane; do not reparent its children.

resource foundryEndpointValues 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = [for backend in foundryBackends: {
  parent: legacyConsumptionApim
  name: 'foundry-${backend.region}-endpoint'
  properties: {
    displayName: 'foundry-${backend.region}-endpoint'
    secret: false
    value: endsWith(backend.endpoint, '/') ? substring(backend.endpoint, 0, length(backend.endpoint) - 1) : backend.endpoint
  }
}]

var endpointSelectionFragmentDefinitions = [
  {
    baseName: 'endpoint_selection_catalog_0_32'
    description: 'Generated model/deployment catalog chunk 0 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-0.xml')
  }
  {
    baseName: 'endpoint_selection_catalog_1_32'
    description: 'Generated model/deployment catalog chunk 1 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-1.xml')
  }
  {
    baseName: 'endpoint_selection_catalog_2_32'
    description: 'Generated model/deployment catalog chunk 2 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-2.xml')
  }
  {
    baseName: 'endpoint_selection_catalog_3_32'
    description: 'Generated model/deployment catalog chunk 3 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-3.xml')
  }
  {
    baseName: 'endpoint_selection_setup_32'
    description: 'Generated model routing setup for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints.xml')
  }
]

var priorityPolicyFragmentDefinitions = [
  {
    baseName: 'simplel7proxy_inbound_pre_32'
    description: 'SimpleL7Proxy inbound policies before model catalog initialization.'
    value: loadTextContent('../policies/simplel7proxy_inbound_pre_32.xml')
  }
  {
    baseName: 'simplel7proxy_inbound_post_32'
    description: 'SimpleL7Proxy inbound policies after model catalog initialization.'
    value: loadTextContent('../policies/simplel7proxy_inbound_post_32.xml')
  }
  {
    baseName: 'simplel7proxy_backend_32'
    description: 'SimpleL7Proxy backend retry and routing policies.'
    value: loadTextContent('../policies/simplel7proxy_backend_32.xml')
  }
  {
    baseName: 'simplel7proxy_outbound_32'
    description: 'SimpleL7Proxy outbound policies.'
    value: loadTextContent('../policies/simplel7proxy_outbound_32.xml')
  }
  {
    baseName: 'simplel7proxy_on_error_32'
    description: 'SimpleL7Proxy error handling policies.'
    value: loadTextContent('../policies/simplel7proxy_on_error_32.xml')
  }
]

var modelPolicyFragmentDefinitions = concat(
  endpointSelectionFragmentDefinitions,
  priorityPolicyFragmentDefinitions
)

// Content-addressed names prevent mixed generations during incremental deploys.
// Superseded generations are retained for rollback and cleaned up only through
// the explicit post-stabilization procedure in docs/runbooks/deployment.md.
var modelApiPolicyTemplate = loadTextContent('../policies/simplel7proxy-priority-policy.xml')
var modelApiPolicyValue = reduce(
  modelPolicyFragmentDefinitions,
  modelApiPolicyTemplate,
  (policy, definition) => replace(
    policy,
    definition.baseName,
    '${definition.baseName}-${uniqueString(definition.value)}'
  )
)

resource modelPolicyFragments 'Microsoft.ApiManagement/service/policyFragments@2024-05-01' = [for definition in modelPolicyFragmentDefinitions: {
  parent: legacyConsumptionApim
  name: '${definition.baseName}-${uniqueString(definition.value)}'
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
  parent: legacyConsumptionApim
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
    value: modelApiPolicyValue
  }
  dependsOn: [
    modelPolicyFragments
  ]
}

// Only the proxy receives this subscription. It is injected from an ACA secret
// into Host1-api-key and never returned to application callers.
resource proxyModelSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: legacyConsumptionApim
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
  parent: legacyConsumptionApim
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
    value: loadTextContent('../policies/realtime-routing-legacy.xml')
  }
  dependsOn: [
    foundryEndpointValues
  ]
}

resource apiRealtimeSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: legacyConsumptionApim
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
  name: guid(foundryAccounts[i].id, legacyConsumptionApim.id, openAiUserRoleId)
  scope: foundryAccounts[i]
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
    principalId: legacyConsumptionApim.identity.principalId
    principalType: 'ServicePrincipal'
  }
}]

resource apimCognitiveUsers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (backend, i) in foundryBackends: {
  name: guid(foundryAccounts[i].id, legacyConsumptionApim.id, cognitiveUserRoleId)
  scope: foundryAccounts[i]
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveUserRoleId)
    principalId: legacyConsumptionApim.identity.principalId
    principalType: 'ServicePrincipal'
  }
}]

// ---------------- Active Basic v2 APIM parity ----------------
// These resources deliberately attach the active HTTP/SSE and realtime configuration
// to the shared Basic v2 APIM adopted by apimcore.bicep. The Consumption service and
// every child above remain untouched for rollback; only callers below move once this
// shared active plane is ready.
resource sharedFoundryEndpointValues 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = [for backend in foundryBackends: {
  parent: sharedApim
  name: 'foundry-${backend.region}-endpoint'
  properties: {
    displayName: 'foundry-${backend.region}-endpoint'
    secret: false
    value: endsWith(backend.endpoint, '/') ? substring(backend.endpoint, 0, length(backend.endpoint) - 1) : backend.endpoint
  }
}]

// WebSocket APIs require a WSS backend. The catalog policy references only these
// named values, so account endpoints stay catalog-derived and never hard-coded.
resource sharedRealtimeWssEndpointValues 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = [for backend in foundryBackends: {
  parent: sharedApim
  name: 'foundry-${backend.region}-realtime-wss-endpoint'
  properties: {
    displayName: 'foundry-${backend.region}-realtime-wss-endpoint'
    secret: false
    value: replace(endsWith(backend.endpoint, '/') ? substring(backend.endpoint, 0, length(backend.endpoint) - 1) : backend.endpoint, 'https://', 'wss://')
  }
}]

resource sharedModelPolicyFragments 'Microsoft.ApiManagement/service/policyFragments@2024-05-01' = [for definition in modelPolicyFragmentDefinitions: {
  parent: sharedApim
  name: '${definition.baseName}-${uniqueString(definition.value)}'
  properties: {
    description: definition.description
    format: 'rawxml'
    value: definition.value
  }
  dependsOn: [
    sharedFoundryEndpointValues
  ]
}]

resource sharedModelsApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: sharedApim
  name: 'openai'
  properties: {
    displayName: 'SimpleL7Proxy model backend'
    path: 'openai'
    protocols: [
      'https'
    ]
    serviceUrl: foundryOpenAiUrl
    subscriptionRequired: true
    apiType: 'http'
  }
}

resource sharedModelOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [for method in modelMethods: {
  parent: sharedModelsApi
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

resource sharedModelsApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: sharedModelsApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: modelApiPolicyValue
  }
  dependsOn: [
    sharedModelPolicyFragments
  ]
}

// This scoped key is injected only into SimpleL7Proxy's Host1 configuration.
resource sharedProxyModelSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: sharedApim
  name: 'ai4ia-proxy-models'
  properties: {
    displayName: 'AI4IA SimpleL7Proxy model hop'
    scope: sharedModelsApi.id
    state: 'active'
    allowTracing: false
  }
  dependsOn: [
    sharedModelsApiPolicy
    sharedModelOperations
  ]
}

// The API-to-proxy credential is deliberately an APIM subscription to a product
// with no APIs. It authenticates only at SimpleL7Proxy and cannot invoke models.
resource sharedProxyIngressProduct 'Microsoft.ApiManagement/service/products@2024-05-01' = {
  parent: sharedApim
  name: 'ai4ia-proxy-ingress'
  properties: {
    displayName: 'AI4IA FastAPI proxy ingress'
    description: 'Opaque credential accepted only by SimpleL7Proxy.'
    subscriptionRequired: true
    approvalRequired: false
    state: 'published'
  }
}

resource sharedProxyIngressSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: sharedApim
  name: 'ai4ia-api-proxy-ingress'
  properties: {
    displayName: 'AI4IA FastAPI proxy ingress credential'
    scope: '/products/ai4ia-proxy-ingress'
    state: 'active'
    allowTracing: false
  }
  dependsOn: [
    sharedProxyIngressProduct
  ]
}

// A WebSocket API has APIM's generated onHandshake operation; no HTTP GET
// operation is declared here. serviceUrl and the routing policy use WSS exactly.
resource sharedRealtimeApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: sharedApim
  name: 'openai-realtime'
  properties: {
    displayName: 'FastAPI realtime relay backend'
    path: 'openai/realtime'
    protocols: [
      'wss'
    ]
    serviceUrl: primaryFoundryRealtimeWssUrl
    subscriptionRequired: true
    type: 'websocket'
  }
  dependsOn: [
    sharedRealtimeWssEndpointValues
  ]
}

// APIM does not allow policies at API scope for WebSocket APIs. It creates the
// immutable onHandshake operation with the API; attach supported policies there.
resource sharedRealtimeHandshake 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' existing = {
  parent: sharedRealtimeApi
  name: 'onHandshake'
}

resource sharedRealtimeApiPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = {
  parent: sharedRealtimeHandshake
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/realtime-routing.xml')
  }
  dependsOn: [
    sharedRealtimeWssEndpointValues
  ]
}

resource sharedApiRealtimeSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: sharedApim
  name: 'ai4ia-api-realtime'
  properties: {
    displayName: 'AI4IA FastAPI realtime relay'
    scope: sharedRealtimeApi.id
    state: 'active'
    allowTracing: false
  }
  dependsOn: [
    sharedRealtimeApiPolicy
  ]
}

// Least privilege is granted to the shared active APIM identity only; legacy APIM
// RBAC is retained above so the inactive rollback service remains complete.
resource sharedApimOpenAiUsers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (backend, i) in foundryBackends: {
  name: guid(foundryAccounts[i].id, sharedApimResourceId, openAiUserRoleId)
  scope: foundryAccounts[i]
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
    principalId: sharedApimPrincipalId
    principalType: 'ServicePrincipal'
  }
}]

resource sharedApimCognitiveUsers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (backend, i) in foundryBackends: {
  name: guid(foundryAccounts[i].id, sharedApimResourceId, cognitiveUserRoleId)
  scope: foundryAccounts[i]
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveUserRoleId)
    principalId: sharedApimPrincipalId
    principalType: 'ServicePrincipal'
  }
}]

// ---------------- SimpleL7Proxy Container App ----------------
var hostEnv = [
  {
    name: 'Host1'
    value: 'host=${sharedApimGatewayUrl};mode=apim;probe=/openai/status;processor=OpenAI;api-key-header=Ocp-Apim-Subscription-Key;retryafter=true'
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
  { name: 'ValidateAuthConfig', value: 'enabled=true;mode=key;header=S7P-KEY' }
  { name: 'ValidateAuthKey1', secretRef: 'api-proxy-inbound-key' }
  {
    name: 'StripRequestHeaders'
    value: string([
      'Authorization'
      'S7P-KEY'
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
    value: sharedProxyModelSubscription.listSecrets().primaryKey
  }
  {
    name: 'api-proxy-inbound-key'
    value: sharedProxyIngressSubscription.listSecrets().primaryKey
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
  dependsOn: [
    sharedModelsApiPolicy
    // listSecrets also creates this edge; retain it explicitly to document the
    // no-caller-update-before-secret cutover requirement.
    #disable-next-line no-unnecessary-dependson
    sharedProxyModelSubscription
    #disable-next-line no-unnecessary-dependson
    sharedProxyIngressSubscription
    // The cutover revision must not point at the shared active gateway until its
    // managed identity role assignments have been accepted by ARM. Operators
    // still verify backend auth during the pre-cutover smoke gate because Entra
    // role propagation is eventually consistent.
    #disable-next-line no-unnecessary-dependson
    sharedApimOpenAiUsers
    #disable-next-line no-unnecessary-dependson
    sharedApimCognitiveUsers
  ]
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
  scope: legacyConsumptionApim
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
output legacyConsumptionApimGatewayUrl string = legacyConsumptionApim.properties.gatewayUrl
output apimGatewayUrl string = sharedApimGatewayUrl
output modelGatewayUrl string = '${sharedApimGatewayUrl}/openai'
@secure()
output modelGatewayKey string = sharedProxyModelSubscription.listSecrets().primaryKey
output proxyIngressUrl string = '${proxyUrl}/openai'
@secure()
output proxyIngressKey string = sharedProxyIngressSubscription.listSecrets().primaryKey
output realtimeGatewayUrl string = '${sharedApimGatewayUrl}/openai'
@secure()
output realtimeGatewayKey string = sharedApiRealtimeSubscription.listSecrets().primaryKey
