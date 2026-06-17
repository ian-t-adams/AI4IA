// Secrets + configuration baseline: Key Vault (RBAC) + App Configuration.
@description('Location for the resources.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Workload token (e.g. ai4ia).')
param workload string

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Deterministic alphanumeric suffix for globally-unique names.')
param uniqueSuffix string

@description('Principal IDs granted Key Vault Secrets User + App Config Data Reader (app identities).')
param readerPrincipalIds array = []

@description('''Principal IDs granted Key Vault Secrets Officer (read/write secrets).
The api managed identity gets this only when custom tools / BYO MCP is enabled, so
it can persist per-user MCP connection secrets at runtime (Phase 12B).''')
param secretsOfficerPrincipalIds array = []

@description('''Enable Key Vault purge protection. Default false so the wipe-and-rebuild
workflow can purge + recreate the vault with the same name. Set true for production.''')
param enablePurgeProtection bool = false

var keyVaultName = take('kv${replace(workload, '-', '')}${uniqueSuffix}', 24)

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: enablePurgeProtection ? true : null
    publicNetworkAccess: 'Enabled'
  }
}

resource appConfig 'Microsoft.AppConfiguration/configurationStores@2024-05-01' = {
  name: take('appcs-${workload}-${environmentName}-${uniqueSuffix}', 50)
  location: location
  tags: tags
  sku: {
    name: 'standard'
  }
  properties: {
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

// Built-in role IDs
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6' // Key Vault Secrets User
var kvSecretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7' // Key Vault Secrets Officer (built-in role GUID as defined in this Azure environment)
var appConfigDataReaderRoleId = '516239f1-63e1-4d78-a4de-a74fb236a071' // App Configuration Data Reader

resource kvRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in readerPrincipalIds: {
  name: guid(keyVault.id, pid, kvSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

resource kvSecretsOfficerAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in secretsOfficerPrincipalIds: {
  name: guid(keyVault.id, pid, kvSecretsOfficerRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsOfficerRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

resource appConfigRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in readerPrincipalIds: {
  name: guid(appConfig.id, pid, appConfigDataReaderRoleId)
  scope: appConfig
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', appConfigDataReaderRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

output keyVaultName string = keyVault.name
output keyVaultId string = keyVault.id
output keyVaultUri string = keyVault.properties.vaultUri
output appConfigName string = appConfig.name
output appConfigId string = appConfig.id
output appConfigEndpoint string = appConfig.properties.endpoint
