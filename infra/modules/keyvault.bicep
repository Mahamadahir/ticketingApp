// Key Vault holding every secret the apps need. Container Apps read the
// values directly through their own managed identity, so nothing sensitive
// sits in the app definition or the repository. Each identity gets read access
// to the individual secrets it needs, not to the whole vault.

param location string
param tags object
param name string
param purgeProtection bool

@description('Which identity may read which secrets: [{ principalId, secrets: [name, ...] }]. Every secret listed must exist.')
param secretReaders array

@secure()
param databaseUrl string
@secure()
@description('Signs the web app session cookie. Only the web app may read it.')
param sessionSecretKey string
@secure()
@description('Satisfies the settings check in apps that never serve a session (public, worker). Different from the web key, so reading it forges nothing.')
param internalSessionSecretKey string
@secure()
param graphWebhookClientState string
@secure()
param ssoClientSecret string
@secure()
param graphClientSecret string

var secretsUserRoleId = '4633458b-17de-406a-b58b-ae56cf9b3dff'

// One entry per (identity, secret) pair.
var grants = flatten(map(secretReaders, reader => map(reader.secrets, secretName => {
  secretName: secretName
  principalId: reader.principalId
})))

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

resource internalSessionSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'session-secret-key-internal'
  properties: {
    value: internalSessionSecretKey
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

// Read access is granted on each secret, so a compromised app can read only
// what its own definition lists. Incremental deployments only add: removing a
// secret from an identity's list does not delete the old assignment (README).
resource readTargets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = [
  for reader in grants: {
    parent: vault
    name: reader.secretName
  }
]

resource readers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (reader, i) in grants: {
    scope: readTargets[i]
    name: guid(readTargets[i].id, reader.principalId, secretsUserRoleId)
    properties: {
      principalId: reader.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRoleId)
    }
    dependsOn: [
      databaseUrlSecret
      sessionSecret
      internalSessionSecret
      webhookState
      ssoSecret
      graphSecret
    ]
  }
]

output name string = vault.name
// Ends with a slash.
output vaultUri string = vault.properties.vaultUri
