// Cost guardrail: a resource-group-scoped consumption budget with optional alerts.
@description('Budget name.')
param name string

@description('Monthly budget amount in the billing currency.')
param amount int = 1000

@description('Budget start date (first of a month, yyyy-MM-dd). Immutable once the budget exists: Azure rejects changing it, so callers should pin a fixed month rather than let it drift. Defaults to the first of the current month for greenfield creation.')
param startDate string = utcNow('yyyy-MM-01')

@description('Email addresses notified when thresholds are crossed (empty = tracking only).')
param alertEmails array = []

@description('Percent-of-budget thresholds that trigger notifications.')
param thresholds array = [
  50
  80
  100
]

// Build a notification per (threshold) only when at least one email is supplied.
var notifications = empty(alertEmails) ? {} : toObject(thresholds, t => 'pct_${t}', t => {
  enabled: true
  operator: 'GreaterThanOrEqualTo'
  threshold: t
  contactEmails: alertEmails
  thresholdType: 'Actual'
})

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: name
  properties: {
    category: 'Cost'
    amount: amount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: startDate
    }
    notifications: notifications
  }
}

output budgetName string = budget.name
