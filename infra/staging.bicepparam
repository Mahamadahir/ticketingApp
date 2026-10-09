using 'main.bicep'

param environmentName = 'staging'

// First deploy: platform only. Flip to true once an image is in the registry
// (the deploy workflow passes image and deployApps itself).
param deployApps = false
param image = readEnvironmentVariable('IMAGE', '')

// Turn on as the app grows the matching mode (see docs/plan.md).
param deployPublicApp = false
param deployWorker = false

// Office and VPN ranges. Empty = open, acceptable only while staging holds
// nothing and sits on its generated URL.
param webAllowedCidrs = []
param publicBaseUrl = ''

param alertEmail = readEnvironmentVariable('ALERT_EMAIL', 'changeme@example.org')

param ssoTenantId = readEnvironmentVariable('SSO_TENANT_ID', '')
param ssoClientId = readEnvironmentVariable('SSO_CLIENT_ID', '')
param graphTenantId = readEnvironmentVariable('GRAPH_TENANT_ID', '')
param graphClientId = readEnvironmentVariable('GRAPH_CLIENT_ID', '')
param graphSharedMailbox = readEnvironmentVariable('GRAPH_SHARED_MAILBOX', '')
param superAdminEmails = readEnvironmentVariable('SUPER_ADMIN_EMAILS', '')

// Secrets come from the environment at deploy time. Never write values here.
param postgresAdminPassword = readEnvironmentVariable('POSTGRES_ADMIN_PASSWORD')
param sessionSecretKey = readEnvironmentVariable('SESSION_SECRET_KEY')
param graphWebhookClientState = readEnvironmentVariable('GRAPH_WEBHOOK_CLIENT_STATE')
param ssoClientSecret = readEnvironmentVariable('SSO_CLIENT_SECRET', '')
param graphClientSecret = readEnvironmentVariable('GRAPH_CLIENT_SECRET', '')

// Small and cheap: staging holds test data only.
param postgresSku = 'Standard_B1ms'
param postgresTier = 'Burstable'
param postgresBackupRetentionDays = 7
param postgresHighAvailability = 'Disabled'
param storageSku = 'Standard_LRS'
param purgeProtection = false
