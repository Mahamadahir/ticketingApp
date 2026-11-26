// The route from the app to Exchange Online for scheduled mailbox changes (forwarding, full access, Send As).
// The app never holds Exchange rights. This Automation account does, through its own system-assigned managed
// identity (never shared with the web, public or worker identities), and it only ever runs the fixed runbooks below.
// The worker's identity may start jobs here and read their output (Automation Job Operator), nothing else.
//
// What this template cannot do, and the owner does by hand (infra/README.md, "Mailbox changes: the Automation
// account"): give the identity the Exchange.ManageAsApp application permission and an Exchange directory role.
// Both need a Global or Privileged Role Administrator. No secret exists anywhere in this module.
//
// The runbooks are created here (type, runtime) but their script text is published by the Deploy workflow from
// infra/runbooks/*.ps1 (.github/scripts/publish-runbooks.sh): ARM can only take runbook content from a URL, and
// the repository is private.

param location string
param tags object
param name string

@description('Principal id of the worker\'s identity: it gets Automation Job Operator on this account only. Empty: no assignment (the apps are not deployed yet).')
param workerPrincipalId string = ''
@description('Write the role assignment. Off in the Deploy workflow (its identity cannot write role assignments); the owner deploys with it on.')
param assignRoles bool = true

@description('Pinned version of the ExchangeOnlineManagement module from the PowerShell Gallery. Change it deliberately and test the runbooks against a test mailbox.')
param exchangeModuleVersion string = '3.9.2'
@description('Version of the Az default package the PowerShell 7.4 runtime environment requires (the runbooks do not use it).')
param azPackageVersion string = '12.3.0'

// The runbooks (file name without .ps1 = runbook name). The list is the whole reach of the worker's role:
// none takes a script or a free-form command.
var runbookNames = [
  'Set-MailForwarding'
  'Clear-MailForwarding'
  'Grant-FullAccess'
  'Remove-FullAccess'
  'Grant-SendAs'
  'Remove-SendAs'
  'Read-MailboxChange'
]
var runtimeEnvironmentName = 'PowerShell-7-4'

// Automation Job Operator: create and manage jobs and read their output, at the account scope.
// https://learn.microsoft.com/azure/automation/automation-role-based-access-control#automation-job-operator
var automationJobOperatorRoleId = '4fe576fe-1146-4730-92eb-48519fa6bf9f'

resource account 'Microsoft.Automation/automationAccounts@2023-11-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    sku: {
      name: 'Basic'
    }
    // Local (account key) authentication is not needed: the worker uses Entra and ARM.
    disableLocalAuth: true
    publicNetworkAccess: true
  }
}

resource runtime 'Microsoft.Automation/automationAccounts/runtimeEnvironments@2024-10-23' = {
  parent: account
  name: runtimeEnvironmentName
  location: location
  tags: tags
  properties: {
    runtime: {
      language: 'PowerShell'
      version: '7.4'
    }
    defaultPackages: {
      Az: azPackageVersion
    }
  }
}

resource exchangeModule 'Microsoft.Automation/automationAccounts/runtimeEnvironments/packages@2024-10-23' = {
  parent: runtime
  name: 'ExchangeOnlineManagement'
  properties: {
    contentLink: {
      uri: 'https://www.powershellgallery.com/api/v2/package/ExchangeOnlineManagement/${exchangeModuleVersion}'
      version: exchangeModuleVersion
    }
  }
}

resource runbooks 'Microsoft.Automation/automationAccounts/runbooks@2024-10-23' = [
  for runbookName in runbookNames: {
    parent: account
    name: runbookName
    location: location
    tags: tags
    properties: {
      runbookType: 'PowerShell'
      runtimeEnvironment: runtime.name
      description: 'Scheduled mailbox changes (infra/runbooks/${runbookName}.ps1). Fixed parameters only.'
      logProgress: false
      logVerbose: false
      draft: {}
    }
    dependsOn: [
      exchangeModule
    ]
  }
]

resource workerJobOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (assignRoles && !empty(workerPrincipalId)) {
  scope: account
  name: guid(account.id, workerPrincipalId, automationJobOperatorRoleId)
  properties: {
    principalId: workerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', automationJobOperatorRoleId)
  }
}

output id string = account.id
output name string = account.name
@description('The object id of the account\'s identity: what the Exchange.ManageAsApp permission and the Exchange directory role are granted to (by hand).')
output identityPrincipalId string = account.identity.principalId
output runbookNames array = runbookNames
