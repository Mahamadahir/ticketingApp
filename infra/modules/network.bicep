// Virtual network for the Container Apps environment and the database.
// The database has no public endpoint: it lives in a delegated subnet and is
// reachable only from inside this network.

param location string
param tags object
param prefix string
param environmentName string

var vnetName = 'vnet-${prefix}-${environmentName}'
var dnsZoneName = '${prefix}-${environmentName}.private.postgres.database.azure.com'

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.20.0.0/16']
    }
    subnets: [
      {
        name: 'snet-container-apps'
        properties: {
          addressPrefix: '10.20.0.0/24'
          delegations: [
            {
              name: 'container-apps'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'snet-postgres'
        properties: {
          addressPrefix: '10.20.1.0/28'
          delegations: [
            {
              name: 'postgres'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

resource dnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: dnsZoneName
  location: 'global'
  tags: tags
}

resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: dnsZone
  name: 'link-${vnetName}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

output containerAppsSubnetId string = '${vnet.id}/subnets/snet-container-apps'
output postgresSubnetId string = '${vnet.id}/subnets/snet-postgres'
// The whole module (zone and VNet link) finishes before the database module
// starts, so the zone is attached by the time the server is created.
output postgresDnsZoneId string = dnsZone.id
