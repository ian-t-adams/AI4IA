// Next.js frontend (app/web) running on Container Apps.
// azd builds app/web/Dockerfile, pushes to ACR, and deploys into this app
// (matched by the `azd-service-name: web` tag). The web server proxies
// browser calls to the api same-origin (see app/web/src/app/api/[...path]),
// so the api base URL is a server-side env var, never exposed to the client.
@description('Location for the web container app.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Container Apps managed environment resource ID.')
param containerEnvId string

@description('Central Log Analytics workspace resource ID for diagnostic settings.')
param logAnalyticsWorkspaceId string

@description('Resource ID of the web user-assigned identity.')
param webIdentityResourceId string

@description('ACR login server the web image is pulled from.')
param acrLoginServer string

@description('Container image for the web app; azd replaces the default with the built app/web image.')
param webImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Base URL of the api the web server proxies to (no trailing /api).')
param apiBaseUrl string

@description('Application runtime environment (dev|prod).')
@allowed([
  'dev'
  'prod'
])
param appEnvironment string = 'dev'

@description('Dev user identity the web proxy injects as X-Dev-User (dev/demo only; ignored in prod).')
param devUser string = ''

@description('Custom domain bound to the web ingress (empty disables custom-domain binding).')
param customDomain string = ''

@description('Existing Azure-managed certificate name to adopt (empty derives a stable name).')
param managedCertificateName string = ''

@description('Container Apps managed environment name (parent of the managed certificate).')
param containerEnvName string

@description('Frontend auth provider (dev|entra). entra turns on MSAL sign-in in the browser.')
@allowed([
  'dev'
  'entra'
])
param authProvider string = 'dev'

@description('Entra SPA app registration client ID (required when authProvider == entra).')
param entraClientId string = ''

@description('Entra tenant ID for the SPA authority (required when authProvider == entra).')
param entraTenantId string = ''

@description('API scope the SPA requests (e.g. api://<api-app-id>/.default; required when authProvider == entra).')
param entraApiScope string = ''

@description('Enable the Voice Live browser control. Default OFF (no live-voice UI is surfaced).')
param voiceLiveEnabled bool = false

@description('Advertise governed tool calling for live-voice sessions to the browser (mirrors the API realtimeToolsEnabled). Default OFF: the panel never offers the tools opt-in.')
param voiceLiveToolsEnabled bool = false

@description('Public URL of the API external ingress the browser opens the live-voice WebSocket against (converted to wss in the browser). Required when voiceLiveEnabled.')
param apiPublicUrl string = ''

@description('Enable the document-library browser UI. Default OFF (no library control is surfaced).')
param documentLibraryEnabled bool = false

@description('Enable the custom-tools / BYO remote-MCP browser UI. Default OFF (no custom-tools control is surfaced).')
param customToolsEnabled bool = false

// Entra sign-in is only wired when the provider is entra AND all three values are
// present; otherwise the web stays in dev mode (matching the frontend's fail-open
// default), so a partial config can never half-enable the sign-in gate.
var entraReady = authProvider == 'entra' && !empty(entraClientId) && !empty(entraTenantId) && !empty(entraApiScope)
var entraEnv = entraReady ? [
  {
    name: 'WEB_AUTH_PROVIDER'
    value: 'entra'
  }
  {
    name: 'ENTRA_CLIENT_ID'
    value: entraClientId
  }
  {
    name: 'ENTRA_TENANT_ID'
    value: entraTenantId
  }
  {
    name: 'ENTRA_API_SCOPE'
    value: entraApiScope
  }
] : []

// In dev/demo (no Entra) the web proxy stamps a fixed user so the dev-auth api
// has a stable identity. Never inject a dev user in prod or once Entra is on.
var injectDevUser = appEnvironment != 'prod' && !empty(devUser) && !entraReady
var devUserEnv = injectDevUser ? [
  {
    name: 'DEV_USER'
    value: devUser
  }
] : []

// Voice Live is only surfaced to the browser when the flag is on AND a
// public API URL is supplied (the browser opens the WS directly against the API
// ingress). Both are emitted together or not at all, so a half-config can never
// half-enable the live-voice control. Default OFF -> no env, no UI, no change.
var voiceLiveReady = voiceLiveEnabled && !empty(apiPublicUrl)
var voiceLiveEnv = voiceLiveReady ? [
  {
    name: 'VOICE_LIVE_ENABLED'
    value: 'true'
  }
  {
    name: 'API_PUBLIC_URL'
    value: apiPublicUrl
  }
] : []

// Governed live-voice tools are advertised to the browser only when live voice is
// itself surfaced AND the tools flag is on (mirrors the API's realtimeToolsEnabled,
// driven from the same root param). Default OFF -> no env -> the panel never offers
// the opt-in, and the relay stays tool-free even if a client forces ?tools=1.
var voiceLiveToolsEnv = (voiceLiveReady && voiceLiveToolsEnabled) ? [
  {
    name: 'VOICE_LIVE_TOOLS_ENABLED'
    value: 'true'
  }
] : []
// flag is on. The library API itself goes through the same-origin Next proxy (no
// public URL needed, unlike the live-voice WebSocket). Default OFF -> no env, no
// control, no change to the chat UI.
var documentLibraryEnv = documentLibraryEnabled ? [
  {
    name: 'DOCUMENT_LIBRARY_ENABLED'
    value: 'true'
  }
] : []

// The custom-tools / BYO remote-MCP UI is only surfaced to the browser
// when the flag is on. The MCP-server API goes through the same-origin Next proxy
// (no public URL needed, like the library). Default OFF -> no env, no control, no
// change to the chat UI. Driven by the same infra flag as the API's
// AI4IA_CUSTOM_TOOLS_ENABLED.
var customToolsEnv = customToolsEnabled ? [
  {
    name: 'CUSTOM_TOOLS_ENABLED'
    value: 'true'
  }
] : []

var webEnv = concat([
  {
    name: 'PORT'
    value: '8080'
  }
  {
    name: 'NODE_ENV'
    value: 'production'
  }
  {
    name: 'API_BASE_URL'
    value: apiBaseUrl
  }
], entraEnv, devUserEnv, voiceLiveEnv, voiceLiveToolsEnv, documentLibraryEnv, customToolsEnv)

// Custom-domain binding. The Azure-managed cert lives at the environment scope and
// is referenced from the app ingress so `azd provision` keeps the binding durable
// instead of wiping an imperatively-added hostname on every deploy. The cert name is
// parameterized so the current environment adopts its existing managed cert (no
// re-issue / duplicate subject) while greenfield derives a stable name.
var webManagedCertName = !empty(managedCertificateName) ? managedCertificateName : 'mc-${replace(customDomain, '.', '-')}'

resource managedEnv 'Microsoft.App/managedEnvironments@2024-10-02-preview' existing = {
  name: containerEnvName
}

resource webCert 'Microsoft.App/managedEnvironments/managedCertificates@2024-10-02-preview' = if (!empty(customDomain)) {
  parent: managedEnv
  name: webManagedCertName
  location: location
  properties: {
    subjectName: customDomain
    domainControlValidation: 'CNAME'
  }
}

var webCustomDomains = empty(customDomain) ? [] : [
  {
    name: customDomain
    bindingType: 'SniEnabled'
    certificateId: webCert.id
  }
]

resource webApp 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-web-${environmentName}'
  location: location
  tags: union(tags, {
    'azd-service-name': 'web'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${webIdentityResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerEnvId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        // Public frontend for the application.
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        customDomains: webCustomDomains
      }
      registries: [
        {
          server: acrLoginServer
          identity: webIdentityResourceId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: webImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: webEnv
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

// Per-app metrics for the web container app. Console/system logs already stream to
// LA via the managed environment's appLogsConfiguration (container-app logs are
// env-scoped only); this adds the per-app metric signal (HTTP 5xx, replica restarts,
// CPU/memory) into the same workspace for correlation.
resource webDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: webApp
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    metrics: [
      { category: 'AllMetrics', enabled: true }
    ]
  }
}

output webAppName string = webApp.name
output webUrl string = 'https://${webApp.properties.configuration.ingress.fqdn}'
