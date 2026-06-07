// Container platform: Azure Container Registry + Container Apps managed environment.
// The app services (web/api/proxy) are deployed onto this environment by azd in later phases.
@description('Location for the platform resources.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Workload token (e.g. ai4ia).')
param workload string

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Deterministic alphanumeric suffix for globally-unique names.')
@minLength(8)
param uniqueSuffix string

@description('Existing Log Analytics workspace name for container app logs.')
param logAnalyticsName string

@description('Principal IDs granted AcrPull (app identities that run images).')
param acrPullPrincipalIds array = []

var acrName = take('acr${replace(workload, '-', '')}${uniqueSuffix}', 50)

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull

resource acrPullAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for pid in acrPullPrincipalIds: {
  name: guid(acr.id, pid, acrPullRoleId)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: pid
    principalType: 'ServicePrincipal'
  }
}]

resource containerEnv 'Microsoft.App/managedEnvironments@2024-10-02-preview' = {
  name: 'cae-${workload}-${environmentName}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output containerEnvName string = containerEnv.name
output containerEnvId string = containerEnv.id
output containerEnvDefaultDomain string = containerEnv.properties.defaultDomain
