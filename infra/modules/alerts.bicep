// Starter alerts. More arrive with the features they watch (worker heartbeat,
// Graph subscription expiry, failed jobs): see "Health and monitoring" in
// docs/plan.md.

param tags object
param prefix string
param environmentName string
param actionGroupId string
param postgresServerId string

resource postgresStorage 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-${prefix}-${environmentName}-pg-storage'
  location: 'global'
  tags: tags
  properties: {
    description: 'Database storage is over 80% full.'
    severity: 2
    enabled: true
    scopes: [postgresServerId]
    evaluationFrequency: 'PT15M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'storage-percent'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'storage_percent'
          metricNamespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
          operator: 'GreaterThan'
          threshold: 80
          timeAggregation: 'Average'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroupId
      }
    ]
  }
}

resource postgresCpu 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-${prefix}-${environmentName}-pg-cpu'
  location: 'global'
  tags: tags
  properties: {
    description: 'Database CPU has averaged over 80% for 30 minutes.'
    severity: 3
    enabled: true
    scopes: [postgresServerId]
    evaluationFrequency: 'PT15M'
    windowSize: 'PT30M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'cpu-percent'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'cpu_percent'
          metricNamespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
          operator: 'GreaterThan'
          threshold: 80
          timeAggregation: 'Average'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroupId
      }
    ]
  }
}
