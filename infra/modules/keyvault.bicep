// Key Vault holding every secret the apps need. Container Apps read the
// values directly through the shared identity, so nothing sensitive sits in
// the app definition or the repository.

param location string
param tags object
param name string
param purgeProtection bool
param readerPrincipalId string

@secure()
param databaseUrl string
@secure()
param sessionSecretKey string
@secure()
param graphWebhookClientState string
@secure()
param ssoClientSecret string
@secure()
param graphClientSecret string

var secretsUserRoleId = '4633458b-17de-406a-b58b-ae56cf9b3dff'

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    // Irreversible once on, and it blocks reusing the vault name for 90 days
    // after deletion, so staging leaves it off.
    enablePurgeProtection: purgeProtection ? true : null
    publicNetworkAccess: 'Enabled'
  }
}

resource databaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'database-url'
  properties: {
    value: databaseUrl
  }
}

resource sessionSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'session-secret-key'
  properties: {
    value: sessionSecretKey
  }
}

resource webhookState 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'graph-webhook-client-state'
  properties: {
    value: graphWebhookClientState
  }
}

resource ssoSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(ssoClientSecret)) {
  parent: vault
  name: 'sso-client-secret'
  properties: {
    value: ssoClientSecret
  }
}

resource graphSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(graphClientSecret)) {
  parent: vault
  name: 'graph-client-secret'
  properties: {
    value: graphClientSecret
  }
}

resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, readerPrincipalId, secretsUserRoleId)
  properties: {
    principalId: readerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRoleId)
  }
}

output name string = vault.name
// Ends with a slash.
output vaultUri string = vault.properties.vaultUri
