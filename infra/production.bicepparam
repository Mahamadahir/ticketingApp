using 'main.bicep'

param environmentName = 'production'

// Empty image = platform only (first deploy: the registry has no image yet). The
// deploy workflow sets IMAGE to deploy the apps.
param image = readEnvironmentVariable('IMAGE', '')

// The public app (webhook and approval links) and the worker (Teams, presence, mail) both exist
// now. Production leaves them off until staging has proved them; say DEPLOY_PUBLIC_APP=true and
// DEPLOY_WORKER=true to turn them on.
var deployPublicAppRaw = toLower(trim(readEnvironmentVariable('DEPLOY_PUBLIC_APP', '')))
param deployPublicApp = empty(deployPublicAppRaw) ? false : deployPublicAppRaw == 'true'
var deployWorkerRaw = toLower(trim(readEnvironmentVariable('DEPLOY_WORKER', '')))
param deployWorker = empty(deployWorkerRaw) ? false : deployWorkerRaw == 'true'

// Office and VPN ranges, comma separated, e.g. WEB_ALLOWED_CIDRS=203.0.113.0/24.
// Required: the deploy fails without it, so production is never opened by omission.
param webAllowedCidrs = filter(map(split(readEnvironmentVariable('WEB_ALLOWED_CIDRS'), ','), cidr => trim(cidr)), cidr => !empty(cidr))
// The internal domain of the web app, once DNS and a certificate exist, e.g. https://tickets.example.org,
// and the public app's address for the emailed links. Empty: the generated Azure addresses.
param webAppBaseUrl = readEnvironmentVariable('WEB_BASE_URL', '')
param publicAppBaseUrl = readEnvironmentVariable('PUBLIC_BASE_URL', '')
// Phase 3B/3C worker switches, off unless set to 'true'. Limits left unset keep the app defaults.
param graphSubscriptionEnabled = toLower(trim(readEnvironmentVariable('GRAPH_SUBSCRIPTION_ENABLED', 'false'))) == 'true'
param mailCatchUpEnabled = toLower(trim(readEnvironmentVariable('MAIL_CATCH_UP_ENABLED', 'false'))) == 'true'
param inboundEnabled = toLower(trim(readEnvironmentVariable('INBOUND_ENABLED', 'false'))) == 'true'
var mailSendingRaw = toLower(trim(readEnvironmentVariable('MAIL_SENDING', '')))
param mailSending = empty(mailSendingRaw) ? 'off' : (mailSendingRaw == 'graph' ? 'graph' : 'off')

param alertEmail = readEnvironmentVariable('ALERT_EMAIL')

param ssoTenantId = readEnvironmentVariable('SSO_TENANT_ID', '')
param ssoClientId = readEnvironmentVariable('SSO_CLIENT_ID', '')
param graphTenantId = readEnvironmentVariable('GRAPH_TENANT_ID', '')
param graphClientId = readEnvironmentVariable('GRAPH_CLIENT_ID', '')
param graphSharedMailbox = readEnvironmentVariable('GRAPH_SHARED_MAILBOX', '')
param superAdminEmails = readEnvironmentVariable('SUPER_ADMIN_EMAILS', '')

// Teams posts and out-of-office detection, off unless set to 'true'.
param teamsNewTicketEnabled = toLower(trim(readEnvironmentVariable('TEAMS_NEW_TICKET_ENABLED', 'false'))) == 'true'
param teamsNotificationsEnabled = toLower(trim(readEnvironmentVariable('TEAMS_NOTIFICATIONS_ENABLED', 'false'))) == 'true'
param presenceEnabled = toLower(trim(readEnvironmentVariable('PRESENCE_ENABLED', 'false'))) == 'true'
param mailboxTimeZonesEnabled = toLower(trim(readEnvironmentVariable('MAILBOX_TIME_ZONES_ENABLED', 'false'))) == 'true'
param entraAccountCheckEnabled = toLower(trim(readEnvironmentVariable('ENTRA_ACCOUNT_CHECK_ENABLED', 'false'))) == 'true'

param postgresAdminPassword = readEnvironmentVariable('POSTGRES_ADMIN_PASSWORD')
param sessionSecretKey = readEnvironmentVariable('SESSION_SECRET_KEY')
param internalSessionSecretKey = readEnvironmentVariable('INTERNAL_SESSION_SECRET_KEY')
param graphWebhookClientState = readEnvironmentVariable('GRAPH_WEBHOOK_CLIENT_STATE')
param ssoClientSecret = readEnvironmentVariable('SSO_CLIENT_SECRET', '')
param graphClientSecret = readEnvironmentVariable('GRAPH_CLIENT_SECRET', '')
param teamsNewTicketWebhookUrl = readEnvironmentVariable('TEAMS_NEW_TICKET_WEBHOOK_URL', '')

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
