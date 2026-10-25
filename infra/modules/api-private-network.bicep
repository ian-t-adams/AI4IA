@description('Region shared by the API environment and its Cosmos private endpoint.')
param location string
param tags object
param workload string
param environmentName string
param logAnalyticsName string
param cosmosAccountId string
param vnetAddressPrefix string
param apiSubnetPrefix string
param privateEndpointSubnetPrefix string

var name = '${workload}-${environmentName}-api-private'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

resource apiNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'vnet-${name}-api-nsg-${location}'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource cosmosNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'vnet-${name}-cosmos-nsg-${location}'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${name}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [vnetAddressPrefix] }
    dhcpOptions: { dnsServers: [] }
    subnets: [
      {
        name: 'api'
        properties: {
          addressPrefix: apiSubnetPrefix
          networkSecurityGroup: { id: apiNsg.id }
          delegations: [
            { name: 'container-apps', properties: { serviceName: 'Microsoft.App/environments' } }
          ]
        }
      }
      {
        name: 'cosmos'
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          networkSecurityGroup: { id: cosmosNsg.id }
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.documents.azure.com'
  location: 'global'
  tags: tags
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: name
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${name}-cosmos'
  location: location
  tags: tags
  properties: {
    subnet: { id: '${vnet.id}/subnets/cosmos' }
    privateLinkServiceConnections: [
      {
        name: 'cosmos-sql'
        properties: {
          privateLinkServiceId: cosmosAccountId
          groupIds: ['Sql']
        }
      }
    ]
  }
}

resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'cosmos-sql', properties: { privateDnsZoneId: zone.id } }
    ]
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-10-02-preview' = {
  name: 'cae-${name}'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: '${vnet.id}/subnets/api'
      internal: false
    }
    publicNetworkAccess: 'Enabled'
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}

output environmentId string = environment.id
output virtualNetworkId string = vnet.id
output privateEndpointId string = endpoint.id
output privateDnsZoneId string = zone.id
