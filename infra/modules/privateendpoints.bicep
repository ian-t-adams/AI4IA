// Data-tier private endpoints + DNS zone groups for the network-isolation pass.
// Provisioned only when main.bicep's `vnetIsolationEnabled` flag is on. Each
// target gets a private endpoint in snet-pep and a DNS zone group so its FQDN
// resolves to the private IP from inside the VNet.
@description('Location for the private endpoints.')
param location string

@description('Tags applied to all resources.')
param tags object

@description('Resource ID of the private-endpoint subnet (snet-pep).')
param pepSubnetId string

@description('''Private-endpoint targets. Each item:
  { name, serviceId, groupId, dnsZoneId }
where groupId is the sub-resource (Sql | blob | vault) and dnsZoneId is the
matching private DNS zone. Empty serviceId entries should be filtered by the
caller (e.g. for conditionally-deployed storage accounts).''')
param targets array

resource privateEndpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for t in targets: {
  name: 'pe-${t.name}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: pepSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'pls-${t.name}'
        properties: {
          privateLinkServiceId: t.serviceId
          groupIds: [
            t.groupId
          ]
        }
      }
    ]
  }
}]

resource dnsZoneGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (t, i) in targets: {
  parent: privateEndpoints[i]
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: replace(t.name, '-', '')
        properties: {
          privateDnsZoneId: t.dnsZoneId
        }
      }
    ]
  }
}]
