// Container registry. The deploy workflow pushes images here (its own
// identity needs AcrPush; see infra/README.md). Each app identity can pull.

param location string
param tags object
param name string
@description('Principal ids of the app identities that pull images.')
param pullPrincipalIds array

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in pullPrincipalIds: {
    scope: registry
    name: guid(registry.id, principalId, acrPullRoleId)
    properties: {
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    }
  }
]

output name string = registry.name
output loginServer string = registry.properties.loginServer
