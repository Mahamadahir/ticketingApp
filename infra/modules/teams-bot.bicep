// The Azure Bot resource for the Teams bot (phase 8A): a free (F0), single tenant registration of the bot
// application, with the Teams channel switched on and the messaging endpoint pointing at the public app's
// POST /api/teams/messages. It creates no identity and no secret: the application is either the
// user-assigned managed identity main.bicep makes for it (msaAppType UserAssignedMSI, no secret at all) or an
// Entra app registration the owner made (SingleTenant, whose secret the apps read from Key Vault).

param tags object
param name string

@description('The public app\'s address, e.g. https://respond.example.org. The endpoint is this plus /api/teams/messages.')
param publicAppUrl string

@description('The bot\'s Entra application (client) id. With a managed identity it is that identity\'s client id.')
param appId string
param tenantId string

@allowed(['UserAssignedMSI', 'SingleTenant'])
param appType string
@description('Resource id of the user-assigned managed identity. Required for UserAssignedMSI, ignored otherwise.')
param identityResourceId string = ''

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: name
  location: 'global'
  tags: tags
  sku: {
    name: 'F0'
  }
  kind: 'azurebot'
  properties: {
    displayName: name
    endpoint: '${publicAppUrl}/api/teams/messages'
    msaAppId: appId
    msaAppType: appType
    msaAppTenantId: tenantId
    msaAppMSIResourceId: appType == 'UserAssignedMSI' ? identityResourceId : null
    // Every call to the endpoint is a plain HTTPS POST; nothing is streamed.
    isStreamingSupported: false
  }
}

resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
    }
  }
}

output name string = bot.name
output messagingEndpoint string = bot.properties.endpoint
