targetScope = 'resourceGroup'

@description('Separate SOURCE-tenant operator approval. Creates only a dedicated UAMI, not an application, FIC, role or APIM attachment.')
param createIdentity bool = false

@minLength(3)
@maxLength(20)
param environmentName string

param location string = resourceGroup().location

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (createIdentity) {
  name: 'id-claude-${environmentName}'
  location: location
}

output resourceId string = createIdentity ? identity!.id : ''
output clientId string = createIdentity ? identity!.properties.clientId : ''
output principalId string = createIdentity ? identity!.properties.principalId : ''
