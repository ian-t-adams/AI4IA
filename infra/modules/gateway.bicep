// Phase 1.5 minimal model gateway: SimpleL7Proxy (Container App) behind APIM.
// The backend always calls models through this single front door so model wiring
// isn't duplicated; capacity-sharing/entitlements/quotas layer on in Phase 6.
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

@description('Resource ID of the proxy user-assigned identity.')
param proxyIdentityResourceId string

@description('ACR login server the proxy image is pulled from.')
param acrLoginServer string

@description('Container image for the proxy (placeholder until azd deploys /proxy).')
param proxyImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Foundry account endpoints the proxy routes model traffic to.')
param foundryEndpoints array

@description('Application Insights connection string for proxy telemetry.')
param appInsightsConnectionString string

@description('APIM publisher email (required by APIM).')
param apimPublisherEmail string

@description('APIM publisher org name.')
param apimPublisherName string = 'AI4IA'

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
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

var proxyUrl = 'https://${proxyApp.properties.configuration.ingress.fqdn}'

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
    serviceUrl: proxyUrl
    subscriptionRequired: false
    apiType: 'http'
  }
}

resource modelsApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2023-05-01-preview' = {
  parent: modelsApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: '<policies><inbound><base /><set-header name="x-correlation-id" exists-action="skip"><value>@(context.RequestId.ToString())</value></set-header></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'
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

output proxyAppName string = proxyApp.name
output proxyUrl string = proxyUrl
output apimName string = apim.name
output apimGatewayUrl string = apim.properties.gatewayUrl
output modelGatewayUrl string = '${apim.properties.gatewayUrl}/openai'
