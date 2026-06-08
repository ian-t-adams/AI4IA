// Phase 3: the Next.js frontend (app/web) running on Container Apps.
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

@description('Resource ID of the web user-assigned identity.')
param webIdentityResourceId string

@description('ACR login server the web image is pulled from.')
param acrLoginServer string

@description('Container image for the web app (placeholder until azd deploys app/web).')
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
], entraEnv, devUserEnv)

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

output webAppName string = webApp.name
output webUrl string = 'https://${webApp.properties.configuration.ingress.fqdn}'
