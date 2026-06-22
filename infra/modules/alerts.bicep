// Minimal Azure Monitor alerting baseline (opt-in). Wired only when main.bicep's
// `enableAlerts` is true; default deployments never create these resources, so this
// is purely additive and cannot fail a deploy when no email/threshold is supplied.
@description('Tags applied to all resources.')
param tags object

@description('Workload token (e.g. ai4ia).')
param workload string

@description('Environment name (e.g. ai4ia-dev).')
param environmentName string

@description('Email notified by the action group. Empty => the action group is created with no receiver (alerts still record, just no notification).')
param alertEmail string = ''

@description('Resource ID of the api container app (scope for the 5xx alert).')
param apiContainerAppId string

@description('Resource ID of the Cosmos DB account (scope for the throttling alert).')
param cosmosAccountId string

// Action group: the single notification target the metric alerts fan out to. The
// email receiver is only attached when an address is supplied, so enabling alerts
// without an email is valid (and silent) rather than a deploy error.
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${workload}-${environmentName}'
  location: 'Global'
  tags: tags
  properties: {
    groupShortName: take(workload, 12)
    enabled: true
    emailReceivers: empty(alertEmail) ? [] : [
      {
        name: 'owner-email'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// API 5xx rate: too many server errors out of the backend container app.
resource api5xxAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-${workload}-${environmentName}-api-5xx'
  location: 'global'
  tags: tags
  properties: {
    description: 'Backend api container app is returning 5xx responses.'
    severity: 2
    enabled: true
    scopes: [
      apiContainerAppId
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'api5xx'
          metricNamespace: 'Microsoft.App/containerApps'
          metricName: 'Requests'
          dimensions: [
            {
              name: 'statusCodeCategory'
              operator: 'Include'
              values: [
                '5xx'
              ]
            }
          ]
          operator: 'GreaterThan'
          threshold: 10
          timeAggregation: 'Total'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}

// Cosmos throttling: sustained HTTP 429s indicate the session store is RU-starved.
resource cosmos429Alert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-${workload}-${environmentName}-cosmos-429'
  location: 'global'
  tags: tags
  properties: {
    description: 'Cosmos DB account is throttling requests (HTTP 429).'
    severity: 2
    enabled: true
    scopes: [
      cosmosAccountId
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'cosmos429'
          metricNamespace: 'Microsoft.DocumentDB/databaseAccounts'
          metricName: 'TotalRequests'
          dimensions: [
            {
              name: 'StatusCode'
              operator: 'Include'
              values: [
                '429'
              ]
            }
          ]
          operator: 'GreaterThan'
          threshold: 10
          timeAggregation: 'Count'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}

output actionGroupId string = actionGroup.id
