// Phase 2: the FastAPI backend (app/api) running on Container Apps.
// azd builds app/api/Dockerfile, pushes to ACR, and deploys into this app
// (matched by the `azd-service-name: api` tag).
@description('Location for the api container app.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Container Apps managed environment resource ID.')
param containerEnvId string

@description('Resource ID of the api user-assigned identity.')
param apiIdentityResourceId string

@description('Client ID of the api user-assigned identity (for AZURE_CLIENT_ID / Managed Identity auth).')
param apiIdentityClientId string

@description('ACR login server the api image is pulled from.')
param acrLoginServer string

@description('Container image for the api (placeholder until azd deploys app/api).')
param apiImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Model gateway base URL (APIM front door + /openai).')
param modelGatewayUrl string

@description('Inbound auth mode the api uses when calling the model gateway (none|api_key|bearer). Must not be none in prod.')
@allowed([
  'none'
  'api_key'
  'bearer'
])
param modelGatewayAuthMode string = 'none'

@description('Model gateway API key (only used when modelGatewayAuthMode == api_key). Stored as a Container App secret.')
@secure()
param modelGatewayApiKey string = ''

@description('Cosmos DB account endpoint for the canonical session store.')
param cosmosEndpoint string

@description('Cosmos DB database name.')
param cosmosDatabase string

@description('Memory store backend the api uses (disabled|in_memory|pgvector|mem0).')
@allowed([
  'disabled'
  'in_memory'
  'pgvector'
  'mem0'
])
param memoryStore string = 'disabled'

@description('Postgres Flexible Server FQDN for the pgvector memory store (empty when memory is not pgvector).')
param postgresHost string = ''

@description('Postgres database name for the pgvector memory store.')
param postgresDatabase string = 'mem0'

@description('Postgres AAD role name the api identity connects as (its identity resource name).')
param postgresUser string = ''

@description('Application Insights connection string for api telemetry.')
param appInsightsConnectionString string

@description('Application runtime environment (maps to AI4IA_ENV). One of local|dev|prod.')
@allowed([
  'dev'
  'prod'
])
param appEnvironment string = 'dev'

@description('Auth provider the api enforces (dev|entra).')
@allowed([
  'dev'
  'entra'
])
param authProvider string = 'dev'

@description('Permit the dev auth provider outside local (set true only for non-prod demos without Entra).')
param allowDevAuth bool = true

@description('Entra tenant ID (required when authProvider == entra).')
param entraTenantId string = ''

@description('Entra audience / API app ID URI (required when authProvider == entra).')
param entraAudience string = ''

@description('Comma-separated admin subjects for the entitlement-management API (AI4IA_ADMIN_SUBJECTS).')
param adminSubjects string = ''

@description('Shared secret required for the entitlement-management API under spoofable dev auth (AI4IA_ADMIN_API_SECRET). Stored as a Container App secret.')
@secure()
param adminApiSecret string = ''

@description('Enable the Voice Live (Phase 10) realtime WebSocket relay. Default OFF (the /api/voice/live route refuses, so the app is inert).')
param realtimeEnabled bool = false

@description('Azure OpenAI realtime api-version the relay uses for the upstream WebSocket.')
param realtimeApiVersion string = '2025-04-01-preview'

@description('Comma-separated browser Origin allowlist for the live-voice relay handshake. Required (non-empty) when realtimeEnabled in a deployed env (the relay fails closed otherwise).')
param realtimeAllowedOrigins string = ''

var entraEnv = authProvider == 'entra' ? [
  {
    name: 'AI4IA_ENTRA_TENANT_ID'
    value: entraTenantId
  }
  {
    name: 'AI4IA_ENTRA_AUDIENCE'
    value: entraAudience
  }
] : []

// Gateway API key is held as a Container App secret and referenced by env when present.
var hasGatewayKey = !empty(modelGatewayApiKey)
var gatewaySecrets = hasGatewayKey ? [
  {
    name: 'model-gateway-api-key'
    value: modelGatewayApiKey
  }
] : []
var gatewayKeyEnv = hasGatewayKey ? [
  {
    name: 'AI4IA_MODEL_GATEWAY_API_KEY'
    secretRef: 'model-gateway-api-key'
  }
] : []

// Admin API secret (entitlement management) held as a Container App secret and
// referenced by env when present. Optional: empty means identity-only admin.
var hasAdminSecret = !empty(adminApiSecret)
var adminSecrets = hasAdminSecret ? [
  {
    name: 'admin-api-secret'
    value: adminApiSecret
  }
] : []
var adminEnv = concat(
  empty(adminSubjects) ? [] : [
    {
      name: 'AI4IA_ADMIN_SUBJECTS'
      value: adminSubjects
    }
  ],
  hasAdminSecret ? [
    {
      name: 'AI4IA_ADMIN_API_SECRET'
      secretRef: 'admin-api-secret'
    }
  ] : []
)

// Postgres connection is required for the durable memory backends (the custom
// pgvector store and the real-mem0 store both persist vectors in Postgres).
var pgEnv = (memoryStore == 'pgvector' || memoryStore == 'mem0') ? [
  {
    name: 'AI4IA_POSTGRES_HOST'
    value: postgresHost
  }
  {
    name: 'AI4IA_POSTGRES_DATABASE'
    value: postgresDatabase
  }
  {
    name: 'AI4IA_POSTGRES_USER'
    value: postgresUser
  }
] : []
// mem0-specific: disable the library's PostHog telemetry at the process level
// (belt-and-suspenders; the code also setdefault()s this before importing mem0).
var mem0Env = memoryStore == 'mem0' ? [
  {
    name: 'MEM0_TELEMETRY'
    value: 'false'
  }
] : []
var memoryEnv = concat([
  {
    name: 'AI4IA_MEMORY_STORE'
    value: memoryStore
  }
], pgEnv, mem0Env)

// Voice Live (Phase 10) realtime relay settings. Default OFF: with the flag unset
// the /api/voice/live WebSocket refuses immediately, so the relay is inert and the
// app's default behavior is unchanged. When enabled, the relay reuses the same
// model-gateway URL + credential as chat for the upstream realtime socket; only
// the flag, api-version, and Origin allowlist are realtime-specific env.
var realtimeEnv = realtimeEnabled ? [
  {
    name: 'AI4IA_REALTIME_ENABLED'
    value: 'true'
  }
  {
    name: 'AI4IA_REALTIME_API_VERSION'
    value: realtimeApiVersion
  }
  {
    name: 'AI4IA_REALTIME_ALLOWED_ORIGINS'
    value: realtimeAllowedOrigins
  }
] : []

var apiEnv = concat([
  {
    name: 'PORT'
    value: '8080'
  }
  {
    name: 'AI4IA_ENV'
    value: appEnvironment
  }
  {
    name: 'AI4IA_AUTH_PROVIDER'
    value: authProvider
  }
  {
    name: 'AI4IA_ALLOW_DEV_AUTH'
    value: string(allowDevAuth)
  }
  {
    name: 'AI4IA_MODEL_GATEWAY_URL'
    value: modelGatewayUrl
  }
  {
    name: 'AI4IA_MODEL_GATEWAY_AUTH_MODE'
    value: modelGatewayAuthMode
  }
  {
    name: 'AI4IA_SESSION_STORE'
    value: 'cosmos'
  }
  {
    name: 'AI4IA_COSMOS_ENDPOINT'
    value: cosmosEndpoint
  }
  {
    name: 'AI4IA_COSMOS_DATABASE'
    value: cosmosDatabase
  }
  {
    name: 'AZURE_CLIENT_ID'
    value: apiIdentityClientId
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appInsightsConnectionString
  }
], gatewayKeyEnv, entraEnv, memoryEnv, adminEnv, realtimeEnv)

resource apiApp 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-api-${environmentName}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'api'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${apiIdentityResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnvId
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: concat(gatewaySecrets, adminSecrets)
      ingress: {
        // External for v1 so the api is directly testable before the web app
        // exists. Flip to internal once web is the only public frontend.
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          identity: apiIdentityResourceId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: apiEnv
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

output apiAppName string = apiApp.name
output apiUrl string = 'https://${apiApp.properties.configuration.ingress.fqdn}'
