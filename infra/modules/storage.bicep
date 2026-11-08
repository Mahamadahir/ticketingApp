// Private blob storage for ticket attachments. No account keys, no public
// blobs: the apps that handle files authenticate with their managed identity
// and hand agents short-lived user-delegation SAS links after a visibility check.

param location string
param tags object
param name string
param skuName string
param containerName string
@description('Days after a blob is created before it moves to the cool tier. Never deleted; see the lifecycle policy below.')
@minValue(1)
param coolAfterDays int = 365
@description('Principal ids of the identities that read and write attachments. Only apps that handle files belong here.')
param accessPrincipalIds array

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

// Attachments are kept indefinitely (plan: data retention). Two rules, both
// scoped to the attachments container, and neither deletes a current blob:
// the first moves blobs to the cool tier (still online, so reads and SAS links
// work unchanged); the second drops superseded versions after 90 days.
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'attachments-to-cool-tier'
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
                  daysAfterCreationGreaterThan: coolAfterDays
                }
              }
            }
          }
        }
        {
          name: 'drop-old-versions-after-90-days'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['${containerName}/']
            }
            actions: {
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

resource dataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in accessPrincipalIds: {
    scope: account
    name: guid(account.id, principalId, blobDataContributorRoleId)
    properties: {
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataContributorRoleId)
    }
  }
]

// Needed to request the user delegation key that signs SAS links.
resource delegator 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in accessPrincipalIds: {
    scope: account
    name: guid(account.id, principalId, blobDelegatorRoleId)
    properties: {
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDelegatorRoleId)
    }
  }
]

output name string = account.name
output id string = account.id
