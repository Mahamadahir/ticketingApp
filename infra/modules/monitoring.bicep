// Log Analytics (where logs are stored and queried), Application Insights
// (request, exception and dependency tracing, stored in the same workspace)
// and the action group that alerts notify.

param location string
param tags object
param prefix string
param environmentName string
param retentionDays int
param dailyCapGb int
param alertEmail string

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}-${environmentName}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionDays
    workspaceCapping: {
      dailyQuotaGb: dailyCapGb
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${prefix}-${environmentName}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${prefix}-${environmentName}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: take('tkt${environmentName}', 12)
    enabled: true
    emailReceivers: [
      {
        name: 'it-support'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

output workspaceName string = workspace.name
output workspaceId string = workspace.id
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output actionGroupId string = actionGroup.id
