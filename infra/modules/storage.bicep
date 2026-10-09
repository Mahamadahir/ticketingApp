// Private blob storage for ticket attachments. No account keys, no public
// blobs: the apps authenticate with the shared managed identity and hand
// agents short-lived user-delegation SAS links after a visibility check.

param location string
param tags object
param name string
param skuName string
param containerName string
param accessPrincipalId string

var blobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var blobDelegatorRoleId = 'db58b8e5-c6ad-4a2a-8342-4190687cbf4a'

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: skuName
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: {
      enabled: true
      days: 14
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 14
    }
  }
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

// Attachments are kept indefinitely (plan: data retention). Move to the cool
// tier after a year, and drop superseded versions after 90 days.
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'cool-after-a-year'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['${containerName}/']
            }
            actions: {
              baseBlob: {
                tierToCool: {
                  daysAfterModificationGreaterThan: 365
                }
              }
              version: {
                delete: {
                  daysAfterCreationGreaterThan: 90
                }
              }
            }
          }
        }
      ]
    }
  }
}

resource dataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, accessPrincipalId, blobDataContributorRoleId)
  properties: {
    principalId: accessPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataContributorRoleId)
  }
}

// Needed to request the user delegation key that signs SAS links.
resource delegator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, accessPrincipalId, blobDelegatorRoleId)
  properties: {
    principalId: accessPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDelegatorRoleId)
  }
}

output name string = account.name
output id string = account.id
