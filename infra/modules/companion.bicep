// Optional, admin-only SimpleL7Proxy CompanionApp telemetry console.
// The root module invokes this module only when companionAppEnabled is true and
// every fail-closed prerequisite holds: proxy Event Hub telemetry, an attested
// digest-pinned image, an Entra app registration and a non-empty admin set.
// It is not an azd service; see docs/runbooks/feature-enablement.md.
@description('Location for the console resources.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Container Apps managed environment resource id.')
param containerEnvId string

@description('Name of the environment container registry.')
param acrName string

@description('Login server of the environment container registry.')
param acrLoginServer string

@description('Digest-pinned CompanionApp image reference in the environment registry.')
param image string

@description('Event Hubs namespace that receives SimpleL7Proxy telemetry.')
param eventHubNamespaceName string

@description('Event hub that carries SimpleL7Proxy telemetry.')
param eventHubName string

@description('Entra tenant id that issues admin sign-in tokens.')
param entraTenantId string

@description('Client id of the Entra app registration for CompanionApp sign-in.')
param entraClientId string

@description('Entra group object ids whose members may use the console.')
param adminGroupIds array

@description('Entra user or service principal object ids that may use the console.')
param adminPrincipalIds array

@description('Optional ingress allow-list of admin CIDR ranges; empty means Easy Auth alone.')
param allowedIpRanges array = []

@description('Minimum replicas (0 scales to zero; 1 keeps the in-memory feed collecting).')
@allowed([0, 1])
param minReplicas int = 0

var appName = 'ca-companion-${environmentName}'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull
var eventHubsDataReceiverRoleId = 'a638d3c7-ab3a-418d-83e6-5f17a39d4fde' // Azure Event Hubs Data Receiver

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-companion-${environmentName}'
  location: location
  tags: tags
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, acrPullRoleId)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource namespace 'Microsoft.EventHub/namespaces@2024-01-01' existing = {
  name: eventHubNamespaceName
}

resource telemetryHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' existing = {
  parent: namespace
  name: eventHubName
}

// A dedicated consumer group keeps the console from competing with other readers.
resource consumerGroup 'Microsoft.EventHub/namespaces/eventhubs/consumergroups@2024-01-01' = {
  parent: telemetryHub
  name: 'companion'
}

// Read-only, and scoped to the one telemetry hub rather than the namespace.
resource telemetryReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(telemetryHub.id, identity.id, eventHubsDataReceiverRoleId)
  scope: telemetryHub
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', eventHubsDataReceiverRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource app 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: appName
  location: location
  // Deliberately no azd-service-name tag: azd deploy must never target this app.
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  dependsOn: [
    acrPull
    telemetryReceiver
  ]
  properties: {
    managedEnvironmentId: containerEnvId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        stickySessions: {
          affinity: 'sticky'
        }
        ipSecurityRestrictions: [for (range, i) in allowedIpRanges: {
          name: 'admin-${i}'
          action: 'Allow'
          ipAddressRange: range
        }]
      }
      registries: [
        {
          server: acrLoginServer
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'companion'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'AZURE_TOKEN_CREDENTIALS', value: 'ManagedIdentityCredential' }
            // The app re-checks the platform-authenticated principal against the same
            // admin set and refuses to start without one.
            { name: 'CompanionApp__Admin__GroupIds', value: join(adminGroupIds, ',') }
            { name: 'CompanionApp__Admin__PrincipalIds', value: join(adminPrincipalIds, ',') }
            { name: 'CompanionApp__EventHubMonitor__eventhub_enabled', value: 'true' }
            { name: 'CompanionApp__EventHubMonitor__EventHubNamespace', value: '${eventHubNamespaceName}.servicebus.windows.net' }
            { name: 'CompanionApp__EventHubMonitor__EventHubName', value: eventHubName }
            { name: 'CompanionApp__EventHubMonitor__ConsumerGroup', value: consumerGroup.name }
            { name: 'CompanionApp__EventHubMonitor__StartPosition', value: 'latest' }
          ]
          probes: [
            {
              type: 'Liveness'
              tcpSocket: {
                port: 8080
              }
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              tcpSocket: {
                port: 8080
              }
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: 1
      }
    }
  }
}

// Every request, including the Blazor circuit, must carry an Entra session for an
// explicitly listed admin group or principal. Unauthenticated requests never
// reach the app.
resource auth 'Microsoft.App/containerApps/authConfigs@2024-10-02-preview' = {
  parent: app
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            entraClientId
            'api://${entraClientId}'
          ]
          defaultAuthorizationPolicy: {
            allowedApplications: [
              entraClientId
            ]
            allowedPrincipals: {
              groups: adminGroupIds
              identities: adminPrincipalIds
            }
          }
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

output appName string = app.name
output fqdn string = app.properties.configuration.ingress.fqdn
output principalId string = identity.properties.principalId
