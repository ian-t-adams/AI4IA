// User-assigned managed identities for the application services.
@description('Location for the identities.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Service names that each get a dedicated user-assigned identity.')
param services array = [
  'api'
  'web'
  'proxy'
]

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for s in services: {
  name: 'id-${s}-${environmentName}'
  location: location
  tags: tags
}]

@description('Identity metadata keyed by service, consumed by app/role modules.')
output identities array = [for (s, i) in services: {
  service: s
  name: identities[i].name
  resourceId: identities[i].id
  clientId: identities[i].properties.clientId
  principalId: identities[i].properties.principalId
}]
