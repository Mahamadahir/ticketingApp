using 'main.bicep'

param environmentName = 'production'

param deployApps = false
param image = readEnvironmentVariable('IMAGE', '')

param deployPublicApp = true
param deployWorker = true

// Office and VPN ranges. Fill in before go-live; do not deploy production open.
param webAllowedCidrs = []
// The internal domain, once DNS and a certificate exist, e.g. https://tickets.example.org
param publicBaseUrl = ''

param alertEmail = readEnvironmentVariable('ALERT_EMAIL', 'changeme@example.org')

param ssoTenantId = readEnvironmentVariable('SSO_TENANT_ID', '')
param ssoClientId = readEnvironmentVariable('SSO_CLIENT_ID', '')
param graphTenantId = readEnvironmentVariable('GRAPH_TENANT_ID', '')
param graphClientId = readEnvironmentVariable('GRAPH_CLIENT_ID', '')
param graphSharedMailbox = readEnvironmentVariable('GRAPH_SHARED_MAILBOX', '')
param superAdminEmails = readEnvironmentVariable('SUPER_ADMIN_EMAILS', '')

param postgresAdminPassword = readEnvironmentVariable('POSTGRES_ADMIN_PASSWORD')
param sessionSecretKey = readEnvironmentVariable('SESSION_SECRET_KEY')
param graphWebhookClientState = readEnvironmentVariable('GRAPH_WEBHOOK_CLIENT_STATE')
param ssoClientSecret = readEnvironmentVariable('SSO_CLIENT_SECRET', '')
param graphClientSecret = readEnvironmentVariable('GRAPH_CLIENT_SECRET', '')

// Point-in-time restore window is the recovery point. Backup retention is the
// maximum 35 days; zone-redundant HA is the option if downtime matters.
param postgresSku = 'Standard_B2s'
param postgresTier = 'Burstable'
param postgresStorageGb = 64
param postgresBackupRetentionDays = 35
param postgresHighAvailability = 'Disabled'
param storageSku = 'Standard_ZRS'
param logRetentionDays = 90
param logDailyCapGb = 2
param purgeProtection = true
