using 'main.bicep'

param environmentName = 'staging'

// Empty image = platform only (first deploy: the registry has no image yet). The
// deploy workflow sets IMAGE to deploy the apps.
param image = readEnvironmentVariable('IMAGE', '')

// The public app (webhook and approval links) and the worker (Teams, presence, mail) both exist now.
// Staging runs them by default; DEPLOY_PUBLIC_APP and DEPLOY_WORKER say true or false.
var deployPublicAppRaw = toLower(trim(readEnvironmentVariable('DEPLOY_PUBLIC_APP', '')))
param deployPublicApp = empty(deployPublicAppRaw) ? true : deployPublicAppRaw == 'true'
var deployWorkerRaw = toLower(trim(readEnvironmentVariable('DEPLOY_WORKER', '')))
param deployWorker = empty(deployWorkerRaw) ? true : deployWorkerRaw == 'true'

// Office and VPN ranges, comma separated, e.g. WEB_ALLOWED_CIDRS=203.0.113.0/24,198.51.100.7/32.
// Required. Use 0.0.0.0/0 only as a deliberate choice for a throwaway test.
param webAllowedCidrs = filter(map(split(readEnvironmentVariable('WEB_ALLOWED_CIDRS'), ','), cidr => trim(cidr)), cidr => !empty(cidr))
// Empty: the generated Azure addresses. Set when a custom domain is in place (infra/README.md).
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

// Secrets come from the environment at deploy time. Never write values here.
param postgresAdminPassword = readEnvironmentVariable('POSTGRES_ADMIN_PASSWORD')
param sessionSecretKey = readEnvironmentVariable('SESSION_SECRET_KEY')
param internalSessionSecretKey = readEnvironmentVariable('INTERNAL_SESSION_SECRET_KEY')
param graphWebhookClientState = readEnvironmentVariable('GRAPH_WEBHOOK_CLIENT_STATE')
param ssoClientSecret = readEnvironmentVariable('SSO_CLIENT_SECRET', '')
param graphClientSecret = readEnvironmentVariable('GRAPH_CLIENT_SECRET', '')
param teamsNewTicketWebhookUrl = readEnvironmentVariable('TEAMS_NEW_TICKET_WEBHOOK_URL', '')

// Small and cheap: staging holds test data only.
param postgresSku = 'Standard_B1ms'
param postgresTier = 'Burstable'
param postgresBackupRetentionDays = 7
param postgresHighAvailability = 'Disabled'
param storageSku = 'Standard_ZRS'
param purgeProtection = false
