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

// In dev/demo (no Entra) the web proxy stamps a fixed user so the dev-auth api
// has a stable identity. Never inject a dev user in prod.
var injectDevUser = appEnvironment != 'prod' && !empty(devUser)
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
], devUserEnv)

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
