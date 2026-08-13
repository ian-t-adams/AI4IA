// Governed model data plane:
// HTTP/SSE callers -> SimpleL7Proxy -> APIM -> catalog-driven Foundry backends.
// Realtime WebSockets deliberately bypass the proxy through a separately scoped
// APIM API because SimpleL7Proxy is an HTTP/SSE worker, not a WebSocket proxy.
@description('Location for the gateway resources.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Workload token (e.g. ai4ia). APIM child entities share one flat namespace per service, so every product/subscription name below is derived from this token -- a second workload on the same shared gateway would otherwise collide on all of them.')
param workload string

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

@minLength(1)
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

@description('Custom domain bound to the proxy ingress (empty disables custom-domain binding).')
param customDomain string = ''

@description('Existing Azure-managed certificate name to adopt (empty derives a stable name).')
param managedCertificateName string = ''

@description('Container Apps managed environment name (parent of the managed certificate).')
param containerEnvName string

@description('Provision the additive Speech Voice Live APIM API, subscription, named values, and account-scoped RBAC. Default OFF.')
param speechVoiceLiveEnabled bool = false

@description('Name of the existing AIServices account Speech Voice Live routes to. This is the SAME account already used as a Foundry model backend (see foundryBackends); no new AIServices account is created for this capability.')
param speechVoiceLiveAccountName string

@minLength(1)
@description('Endpoint of the existing AIServices account Speech Voice Live routes to (https://<account>.cognitiveservices.azure.com/ or the .services.ai.azure.com equivalent). Converted to WSS and combined with the fixed /voice-live/realtime path; never a user-suppliable value.')
param speechVoiceLiveAccountEndpoint string

@description('Managed-identity audience (APIM authentication-managed-identity "resource", without the /.default suffix) APIM authenticates to the Speech Voice Live AIServices account with. Defaults to the audience the azure-ai-voicelive SDK requests by default (https://ai.azure.com/.default) for the fixed api-version this module pins. Kept overridable via a named value (never caller-influenced) so a future api-version or account can move it without a code change.')
param speechVoiceLiveManagedIdentityAudience string = 'https://ai.azure.com'

// APIM child entities (products, subscriptions) live in one flat namespace per APIM
// service. This gateway is a shared plane intended to front more than one workload,
// so each name is derived from ${workload} rather than hardcoded. For workload
// 'ai4ia' these still emit the original 'ai4ia-*' names, so an existing deployment
// sees no resource replacement and no subscription-key rotation.
var proxyModelSubscriptionName = '${workload}-proxy-models'
var proxyIngressProductName = '${workload}-proxy-ingress'
var proxyIngressSubscriptionName = '${workload}-api-proxy-ingress'
var realtimeSubscriptionName = '${workload}-api-realtime'
var speechVoiceLiveSubscriptionName = '${workload}-api-speech-voice-live'

var foundryBase = endsWith(primaryFoundryEndpoint, '/') ? primaryFoundryEndpoint : '${primaryFoundryEndpoint}/'
var foundryOpenAiUrl = '${foundryBase}openai'
var primaryFoundryRealtimeWssUrl = '${replace(endsWith(primaryFoundryEndpoint, '/') ? substring(primaryFoundryEndpoint, 0, max(length(primaryFoundryEndpoint) - 1, 0)) : primaryFoundryEndpoint, 'https://', 'wss://')}/openai/realtime'
// Speech Voice Live's backend host, independent of the foundryBackends loop
// above (that loop drives Azure OpenAI realtime routing across every region).
// The same underlying AIServices account may coincide with one of those
// backends, but this module never assumes that -- it only ever talks to the
// account named by speechVoiceLiveAccountName/-Endpoint.
var speechVoiceLiveAccountBase = endsWith(speechVoiceLiveAccountEndpoint, '/') ? substring(speechVoiceLiveAccountEndpoint, 0, max(length(speechVoiceLiveAccountEndpoint) - 1, 0)) : speechVoiceLiveAccountEndpoint
var speechVoiceLiveWssBase = replace(speechVoiceLiveAccountBase, 'https://', 'wss://')
var proxyAppName = 'ca-proxy-${environmentName}'
var proxyContainerConfig = loadJsonContent('../proxy-container-config.json')

// ---------------- APIM trust boundary ----------------
// The shared Basic v2 APIM is created and owned by apimcore.bicep; this module
// only attaches the model, realtime, and Speech Voice Live children to it.
resource sharedApim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: sharedApimName
}

// Backend catalog shards. The count here MUST equal CATALOG_FRAGMENT_COUNT in
// scripts/gen-gateway-policy.py: loadTextContent requires literal paths, so this
// list cannot be generated, and Bicep deploys only what it lists. A shard the
// generator writes but this list omits would drop its models from routing with
// no error. scripts/tests/test_gateway_policy.py pins the two together.
// Shards beyond what the catalog currently needs hold an empty JObject and merge
// as a no-op, so spare capacity costs nothing but a tiny fragment resource.
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
    baseName: 'endpoint_selection_catalog_4_32'
    description: 'Generated model/deployment catalog chunk 4 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-4.xml')
  }
  {
    baseName: 'endpoint_selection_catalog_5_32'
    description: 'Generated model/deployment catalog chunk 5 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-5.xml')
  }
  {
    baseName: 'endpoint_selection_catalog_6_32'
    description: 'Generated model/deployment catalog chunk 6 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-6.xml')
  }
  {
    baseName: 'endpoint_selection_catalog_7_32'
    description: 'Generated model/deployment catalog chunk 7 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-7.xml')
  }
  {
    baseName: 'endpoint_selection_catalog_8_32'
    description: 'Generated model/deployment catalog chunk 8 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-8.xml')
  }
  {
    baseName: 'endpoint_selection_catalog_9_32'
    description: 'Generated model/deployment catalog chunk 9 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-9.xml')
  }
  {
    baseName: 'endpoint_selection_catalog_10_32'
    description: 'Generated model/deployment catalog chunk 10 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-10.xml')
  }
  {
    baseName: 'endpoint_selection_catalog_11_32'
    description: 'Generated model/deployment catalog chunk 11 for SimpleL7Proxy.'
    value: loadTextContent('../policies/simplel7proxy-endpoints-catalog-11.xml')
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

// Normalize checkout-specific CRLF before content-addressing and deployment.
// LF inputs are unchanged, so existing Linux/GitHub fragment names stay stable.
var normalizedModelPolicyFragmentDefinitions = [for definition in modelPolicyFragmentDefinitions: {
  baseName: definition.baseName
  description: definition.description
  value: replace(definition.value, '\r\n', '\n')
}]

// Content-addressed names prevent mixed generations during incremental deploys.
// Superseded generations are retained for rollback and cleaned up only through
// the explicit post-stabilization procedure in docs/runbooks/deployment.md.
var modelApiPolicyTemplate = loadTextContent('../policies/simplel7proxy-priority-policy.xml')
var modelApiPolicyValue = reduce(
  normalizedModelPolicyFragmentDefinitions,
  modelApiPolicyTemplate,
  (policy, definition) => replace(
    policy,
    definition.baseName,
    '${definition.baseName}-${uniqueString(definition.value)}'
  )
)

var modelMethods = [
  'POST'
  'GET'
  'PUT'
  'PATCH'
  'DELETE'
]

// APIM is the only identity granted normal model access by this module.
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var cognitiveUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource foundryAccounts 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = [for backend in foundryBackends: {
  name: backend.accountName
}]

// ---------------- Basic v2 APIM model + realtime plane ----------------
// These resources attach the active HTTP/SSE and realtime configuration to the
// shared Basic v2 APIM adopted by apimcore.bicep.
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

resource sharedModelPolicyFragments 'Microsoft.ApiManagement/service/policyFragments@2024-05-01' = [for definition in normalizedModelPolicyFragmentDefinitions: {
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
  name: proxyModelSubscriptionName
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
  name: proxyIngressProductName
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
  name: proxyIngressSubscriptionName
  properties: {
    displayName: 'AI4IA FastAPI proxy ingress credential'
    scope: '/products/${proxyIngressProductName}'
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
  name: realtimeSubscriptionName
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

// ---------------- Speech Voice Live (additive, isolated) ----------------
// A second, separately scoped realtime provider on the SAME Basic v2 APIM. It is
// entirely additive: it adds one new WebSocket API/path, one new subscription, and
// one new named-scoped role assignment; it does not modify, reparent, or share
// credentials with /openai/realtime, the model/MCP/proxy APIs, or their
// subscriptions above.
resource speechVoiceLiveWssEndpointValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = if (speechVoiceLiveEnabled) {
  parent: sharedApim
  name: 'speech-voice-live-wss-endpoint'
  properties: {
    displayName: 'speech-voice-live-wss-endpoint'
    secret: false
    value: speechVoiceLiveWssBase
  }
}

resource speechVoiceLiveAudienceValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = if (speechVoiceLiveEnabled) {
  parent: sharedApim
  name: 'speech-voice-live-mi-audience'
  properties: {
    displayName: 'speech-voice-live-mi-audience'
    secret: false
    value: speechVoiceLiveManagedIdentityAudience
  }
}

// A WebSocket API has APIM's generated onHandshake operation; no HTTP GET
// operation is declared here, matching the /openai/realtime pattern above.
resource sharedSpeechVoiceLiveApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = if (speechVoiceLiveEnabled) {
  parent: sharedApim
  name: 'speech-voice-live-realtime'
  properties: {
    displayName: 'Speech Voice Live realtime relay backend'
    path: 'speech/voice-live/realtime'
    protocols: [
      'wss'
    ]
    serviceUrl: '${speechVoiceLiveWssBase}/voice-live/realtime'
    subscriptionRequired: true
    type: 'websocket'
  }
  dependsOn: [
    speechVoiceLiveWssEndpointValue
  ]
}

// APIM does not allow policies at API scope for WebSocket APIs; it creates the
// immutable onHandshake operation with the API, as with sharedRealtimeHandshake.
resource sharedSpeechVoiceLiveHandshake 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' existing = if (speechVoiceLiveEnabled) {
  parent: sharedSpeechVoiceLiveApi
  name: 'onHandshake'
}

resource sharedSpeechVoiceLiveApiPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = if (speechVoiceLiveEnabled) {
  parent: sharedSpeechVoiceLiveHandshake
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/speech-voice-live.xml')
  }
  dependsOn: [
    speechVoiceLiveWssEndpointValue
    speechVoiceLiveAudienceValue
  ]
}

// Distinct, API-scoped subscription for FastAPI only. Its key cannot invoke
// /openai/realtime, the normal model API, MCP, or the proxy ingress product --
// each of those is a different subscription scope declared above.
resource sharedSpeechVoiceLiveSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = if (speechVoiceLiveEnabled) {
  parent: sharedApim
  name: speechVoiceLiveSubscriptionName
  properties: {
    displayName: 'AI4IA FastAPI Speech Voice Live relay'
    scope: sharedSpeechVoiceLiveApi.id
    state: 'active'
    allowTracing: false
  }
  dependsOn: [
    sharedSpeechVoiceLiveApiPolicy
  ]
}

// Least privilege: Foundry data-plane roles are granted to the APIM system identity
// only, per Foundry account, so no other principal inherits model access.
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

// Speech Voice Live's own existing AIServices account reference. Declared
// separately from foundryAccounts[] (which is indexed by foundryBackends) so
// this grant is scoped ONLY to the one account Speech Voice Live is approved
// to reach, never broadened to every regional backend.
resource speechVoiceLiveAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = if (speechVoiceLiveEnabled) {
  name: speechVoiceLiveAccountName
}

// Voice Live 2026-04-10 requires both Cognitive Services User and Foundry User
// (formerly Azure AI User). sharedApimCognitiveUsers already grants the first
// role to every account in foundryBackends, including this selected account.
// Add only the second role here to avoid a duplicate role assignment tuple.
resource sharedApimSpeechVoiceLiveFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (speechVoiceLiveEnabled) {
  name: guid(speechVoiceLiveAccount.id, sharedApimResourceId, foundryUserRoleId, 'speech-voice-live')
  scope: speechVoiceLiveAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: sharedApimPrincipalId
    principalType: 'ServicePrincipal'
  }
}

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
  // MUST be `PriorityWorkers` (plural). ConfigParser.ApplyEnv builds
  // PriorityWorkerDict -- the dictionary WorkerFactory actually reserves from --
  // by reading the plural key only. ProxyConfig also declares a SINGULAR
  // `PriorityWorker` string property, but nothing converts it into that
  // dictionary (it is absent from the ApplyDerivedSettingsFromConfigNames list),
  // so the singular name parses cleanly, validates, and is then silently
  // discarded. Verified against the vendored parser: with the singular name the
  // dict stays at its `2:1,3:1` default, which reserves NOTHING for band 1 --
  // the high band this deployment routes admins into. Empty is safe either way
  // (both names fall back to the default), so the disabled path is unchanged.
  { name: 'PriorityWorkers', value: proxyPrioritiesEnabled ? proxyPriorityWorkers : '' }
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
  // ACA calls startup/readiness/liveness every 5/10/30 seconds. SimpleL7Proxy
  // classifies every successful hit as EventType.Probe, so its defaults fan each
  // healthy hit to console, App Insights, and the event logger. The event type
  // does not distinguish success from failure, so suppress it at all three
  // destinations rather than patching the vendored event path. ACA platform
  // health/restart metrics remain enabled below; circuit-breaker, exception, and
  // recovery warning logs remain routed.
  { name: 'LogToConsole', value: string(proxyContainerConfig.logging.logToConsole) }
  { name: 'LogToAI', value: string(proxyContainerConfig.logging.logToAI) }
  {
    name: 'LogToEvents'
    value: string(proxyContainerConfig.logging.logToEvents)
  }
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

var proxyFqdn = toLower(proxyApp.properties.configuration.ingress.fqdn)
var proxyUrl = 'https://${proxyFqdn}'
var effectiveProxyIngressHosts = union(
  [ proxyFqdn ],
  empty(trim(customDomain)) ? [] : [ toLower(trim(customDomain)) ]
)

output proxyAppName string = proxyApp.name
output proxyUrl string = proxyUrl
output apimGatewayUrl string = sharedApimGatewayUrl
output proxyIngressUrl string = '${proxyUrl}/openai'
output proxyIngressHosts string = join(effectiveProxyIngressHosts, ',')
@secure()
output proxyIngressKey string = sharedProxyIngressSubscription.listSecrets().primaryKey
output realtimeGatewayUrl string = '${sharedApimGatewayUrl}/openai'
@secure()
output realtimeGatewayKey string = sharedApiRealtimeSubscription.listSecrets().primaryKey
// Speech Voice Live base URL intentionally omits /realtime (the relay appends
// it), matching the realtimeGatewayUrl convention above exactly.
output speechVoiceLiveGatewayUrl string = speechVoiceLiveEnabled ? '${sharedApimGatewayUrl}/speech/voice-live' : ''
@secure()
#disable-next-line BCP422
output speechVoiceLiveGatewayKey string = speechVoiceLiveEnabled ? sharedSpeechVoiceLiveSubscription.listSecrets().primaryKey : ''
