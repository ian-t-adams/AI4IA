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

@description('''Infrastructure subnet ID for VNet injection (network-isolation pass).
Empty (default) => the env stays on the shared/public network with its current
name (no change). Non-empty => the env is created VNet-injected under a `-vnet`
name. VNet injection is creation-time only, so enabling this provisions a NEW
environment; the apps must be redeployed onto it (see the apply runbook).''')
param infrastructureSubnetId string = ''

var acrName = take('acr${replace(workload, '-', '')}${uniqueSuffix}', 50)

var vnetInjected = !empty(infrastructureSubnetId)

// New resource name when VNet-injected so ARM creates a fresh, injectable env
// instead of attempting an (illegal) in-place vnetConfiguration update.
var containerEnvName = vnetInjected ? 'cae-${workload}-${environmentName}-vnet' : 'cae-${workload}-${environmentName}'

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
  name: containerEnvName
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
    // VNet injection (creation-time only). `internal: false` keeps a public
    // ingress load balancer so users still reach the apps over the internet,
    // while egress flows through snet-infra so the data-tier private endpoints
    // resolve. Omitted entirely when no subnet is supplied (default = unchanged).
    vnetConfiguration: vnetInjected ? {
      infrastructureSubnetId: infrastructureSubnetId
      internal: false
    } : null
  }
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output containerEnvName string = containerEnv.name
output containerEnvId string = containerEnv.id
output containerEnvDefaultDomain string = containerEnv.properties.defaultDomain
