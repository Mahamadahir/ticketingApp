// Ticketing app infrastructure for one environment (staging or production).
// Deploy at resource-group scope: one resource group per environment.
//
//   az deployment group what-if -g <rg> -f infra/main.bicep -p infra/staging.bicepparam
//
// See infra/README.md for the bootstrap order and docs/plan.md for the design.

targetScope = 'resourceGroup'

@allowed(['staging', 'production'])
param environmentName string

param location string = resourceGroup().location

@maxLength(9)
@description('Short name used in resource names. Max 9 characters (Key Vault names are capped at 24).')
param prefix string = 'ticketing'

// ---- What to deploy ---------------------------------------------------------

@description('Full image reference, e.g. <acr>.azurecr.io/ticketing:<sha>. Empty creates the platform only (network, database, storage, vault, registry, monitoring), which is the first deploy because the registry has no image yet. Setting it deploys the apps and the migrate job.')
param image string = ''

@description('Second app (APP_ROLE=public) that serves only the Graph webhook, the emailed approval links and /health, on an address open to the internet. The app mounts only those routes in this mode (app/main.py), so it cannot serve the agent console.')
param deployPublicApp bool = false

@description('Background worker (python -m app.worker): the Teams posts, presence checks, mail and the heartbeat. One replica, no ingress.')
param deployWorker bool = false

// ---- Access -----------------------------------------------------------------

@minLength(1)
@description('Office and VPN ranges allowed to reach the web app, e.g. ["203.0.113.0/24"]. Required, so nobody opens the agent console to the internet by leaving it out. To really allow everyone (a staging test), say so with "0.0.0.0/0".')
param webAllowedCidrs array

@description('Where the WEB app (agent console) is reached, despite the name, e.g. https://tickets.example.org. Empty uses the generated Azure URL. Becomes WEB_BASE_URL and the Entra redirect address.')
param publicBaseUrl string = ''

@description('Where the PUBLIC app is reached, e.g. https://respond.example.org. Empty uses the generated Azure URL. Becomes PUBLIC_BASE_URL, which the emailed approval link is built on.')
param publicAppBaseUrl string = ''

// ---- Sizing -----------------------------------------------------------------

param postgresSku string = 'Standard_B1ms'
param postgresTier string = 'Burstable'
param postgresStorageGb int = 32
@minValue(7)
@maxValue(35)
param postgresBackupRetentionDays int = 7
@allowed(['Disabled', 'ZoneRedundant'])
param postgresHighAvailability string = 'Disabled'

@allowed(['Standard_LRS', 'Standard_ZRS', 'Standard_GRS', 'Standard_GZRS'])
param storageSku string = 'Standard_ZRS'

param webMinReplicas int = 1
param webMaxReplicas int = 2
@description('uvicorn processes per replica. Size with the container CPU below.')
param uvicornWorkers int = 2

param logRetentionDays int = 30
@description('Daily ingestion cap for Log Analytics in GB. -1 for no cap.')
param logDailyCapGb int = 1

param purgeProtection bool = false

// ---- Application settings (non-secret) -------------------------------------

@description('Where database alerts go. Required: there is no default, so a missing value fails the deploy instead of sending alerts to a dead address.')
param alertEmail string
param ssoTenantId string = ''
param ssoClientId string = ''
param graphTenantId string = ''
param graphClientId string = ''
param graphSharedMailbox string = ''
param superAdminEmails string = ''

@allowed(['off', 'graph'])
@description('Whether the worker sends email from the shared mailbox through Graph (phase 3D). graph needs the Graph tenant, client and secret and GRAPH_SHARED_MAILBOX, with Mail.Send granted in Entra and limited to that mailbox. (The app also has a dev mode; it only works in the dev environment and is not allowed here.)')
param mailSending string = 'off'

@description('Teams channel post for each new ticket (phase 2D). The webhook URL secret is also needed, or posts wait in the outbox.')
param teamsNewTicketEnabled bool = false
@description('Also copy an unread notification to Teams after the delay each person has set (phase 2C-ii). Uses the same webhook URL.')
param teamsNotificationsEnabled bool = false
@description('Worker marks agents who are out of office from Graph presence (phase 2E). Needs the Graph settings and the Presence.Read.All and User.Read.All application permissions, granted in Entra.')
param presenceEnabled bool = false

// ---- Secrets (supplied at deploy time, stored in Key Vault) -----------------

@secure()
param postgresAdminPassword string
@secure()
param sessionSecretKey string
@secure()
@description('Session key for the public app and worker, which never serve an agent session but whose settings check needs a non-default value. Generate it separately from SESSION_SECRET_KEY so the two rotate independently and reading this one forges nothing.')
param internalSessionSecretKey string
@secure()
param graphWebhookClientState string
@secure()
@description('Optional until Entra SSO is registered for this environment.')
param ssoClientSecret string = ''
@secure()
@description('Optional until Graph mail is set up (plan: phase 3).')
param graphClientSecret string = ''
@secure()
@description('Teams Workflows webhook URL for the support channel. Optional until Teams posts are switched on; only the worker can read it.')
param teamsNewTicketWebhookUrl string = ''

// ---- Names ------------------------------------------------------------------

var suffix = uniqueString(resourceGroup().id)
var envShort = take(environmentName, 4)
var tags = {
  app: 'ticketing'
  environment: environmentName
}
var dbName = 'ticketing'
var dbAdminLogin = 'ticketing_admin'
var storageContainerName = 'attachments'
var keyVaultName = 'kv-${prefix}-${envShort}-${take(suffix, 6)}'

// Apps exist only when there is an image to run.
var deployApps = !empty(image)

// One identity per app and for the migrate job, so a compromised app can use
// only what its own identity was granted (the public app faces the internet).
resource identityWeb 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-${environmentName}-web'
  location: location
  tags: tags
}

resource identityPublic 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-${environmentName}-public'
  location: location
  tags: tags
}

resource identityWorker 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-${environmentName}-worker'
  location: location
  tags: tags
}

resource identityMigrate 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-${environmentName}-migrate'
  location: location
  tags: tags
}

module network 'modules/network.bicep' = {
  name: 'network'
  params: {
    location: location
    tags: tags
    prefix: prefix
    environmentName: environmentName
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    tags: tags
    prefix: prefix
    environmentName: environmentName
    retentionDays: logRetentionDays
    dailyCapGb: logDailyCapGb
    alertEmail: alertEmail
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  params: {
    location: location
    tags: tags
    name: 'acr${prefix}${environmentName}${suffix}'
    pullPrincipalIds: [
      identityWeb.properties.principalId
      identityPublic.properties.principalId
      identityWorker.properties.principalId
      identityMigrate.properties.principalId
    ]
  }
}

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    location: location
    tags: tags
    name: '${prefix}-${environmentName}-pg-${take(suffix, 4)}'
    databaseName: dbName
    adminLogin: dbAdminLogin
    adminPassword: postgresAdminPassword
    skuName: postgresSku
    skuTier: postgresTier
    storageSizeGb: postgresStorageGb
    backupRetentionDays: postgresBackupRetentionDays
    highAvailability: postgresHighAvailability
    delegatedSubnetId: network.outputs.postgresSubnetId
    privateDnsZoneId: network.outputs.postgresDnsZoneId
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    tags: tags
    name: 'st${prefix}${envShort}${take(suffix, 6)}'
    skuName: storageSku
    containerName: storageContainerName
    // Attachments are handled by the web app (uploads, viewing) and the worker
    // (mail ingestion). The public app and the migrate job never touch files.
    accessPrincipalIds: [
      identityWeb.properties.principalId
      identityWorker.properties.principalId
    ]
  }
}

module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    tags: tags
    name: keyVaultName
    purgeProtection: purgeProtection
    secretReaders: secretReaders
    databaseUrl: 'postgresql+psycopg2://${dbAdminLogin}:${uriComponent(postgresAdminPassword)}@${postgres.outputs.fqdn}:5432/${dbName}?sslmode=require'
    sessionSecretKey: sessionSecretKey
    internalSessionSecretKey: internalSessionSecretKey
    graphWebhookClientState: graphWebhookClientState
    ssoClientSecret: ssoClientSecret
    graphClientSecret: graphClientSecret
    teamsNewTicketWebhookUrl: teamsNewTicketWebhookUrl
  }
}

module appEnvironment 'modules/container-apps-environment.bicep' = {
  name: 'container-apps-environment'
  params: {
    location: location
    tags: tags
    name: 'cae-${prefix}-${environmentName}'
    subnetId: network.outputs.containerAppsSubnetId
    logAnalyticsWorkspaceName: monitoring.outputs.workspaceName
  }
}

module alerts 'modules/alerts.bicep' = {
  name: 'alerts'
  params: {
    tags: tags
    prefix: prefix
    environmentName: environmentName
    actionGroupId: monitoring.outputs.actionGroupId
    postgresServerId: postgres.outputs.id
  }
}

// ---- Apps -------------------------------------------------------------------

// Who may read which secret, and so which secrets each app receives. Container
// Apps read these from Key Vault with the app's own identity. Web holds the
// session key and the SSO secret; nothing else does. The migrate job gets the
// database URL only. The web app also gets the Graph secret and webhook state
// because agents send replies and the settings check wants them together.
var optionalSsoSecret = !empty(ssoClientSecret) ? ['sso-client-secret'] : []
var optionalGraphSecret = !empty(graphClientSecret) ? ['graph-client-secret'] : []
var webSecretNames = concat(
  ['database-url', 'session-secret-key', 'graph-webhook-client-state'],
  optionalSsoSecret,
  optionalGraphSecret
)
// Public receives the webhook and stores it (it checks the client state and writes to the database;
// it makes no Graph calls) and checks the emailed approval links with the internal key the worker
// signs them with. It holds no Graph secret: the worker is the only app that calls Graph with one
// besides web.
var publicSecretNames = ['database-url', 'session-secret-key-internal', 'graph-webhook-client-state']
var optionalTeamsSecret = !empty(teamsNewTicketWebhookUrl) ? ['teams-new-ticket-webhook-url'] : []
// The worker is the only app that posts to Teams.
var workerSecretNames = concat(publicSecretNames, optionalGraphSecret, optionalTeamsSecret)
var migrateSecretNames = ['database-url']

// Handed to the key vault module, which grants each identity read access to
// exactly its listed secrets.
var secretReaders = [
  { principalId: identityWeb.properties.principalId, secrets: webSecretNames }
  { principalId: identityPublic.properties.principalId, secrets: publicSecretNames }
  { principalId: identityWorker.properties.principalId, secrets: workerSecretNames }
  { principalId: identityMigrate.properties.principalId, secrets: migrateSecretNames }
]

var secretEnvNames = {
  'database-url': 'DATABASE_URL'
  'session-secret-key': 'SESSION_SECRET_KEY'
  'session-secret-key-internal': 'SESSION_SECRET_KEY'
  'graph-webhook-client-state': 'GRAPH_WEBHOOK_CLIENT_STATE'
  'sso-client-secret': 'SSO_CLIENT_SECRET'
  'graph-client-secret': 'GRAPH_CLIENT_SECRET'
  'teams-new-ticket-webhook-url': 'TEAMS_NEW_TICKET_WEBHOOK_URL'
}
var vaultSecretsUrl = 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/'

var webSecrets = [for n in webSecretNames: { name: n, keyVaultUrl: '${vaultSecretsUrl}${n}', identity: identityWeb.id }]
var publicSecrets = [for n in publicSecretNames: { name: n, keyVaultUrl: '${vaultSecretsUrl}${n}', identity: identityPublic.id }]
var workerSecrets = [for n in workerSecretNames: { name: n, keyVaultUrl: '${vaultSecretsUrl}${n}', identity: identityWorker.id }]
var migrateSecrets = [for n in migrateSecretNames: { name: n, keyVaultUrl: '${vaultSecretsUrl}${n}', identity: identityMigrate.id }]

// database-url -> DATABASE_URL, session-secret-key -> SESSION_SECRET_KEY, ...
var webSecretEnv = [for n in webSecretNames: { name: secretEnvNames[n], secretRef: n }]
var publicSecretEnv = [for n in publicSecretNames: { name: secretEnvNames[n], secretRef: n }]
var workerSecretEnv = [for n in workerSecretNames: { name: secretEnvNames[n], secretRef: n }]
var migrateSecretEnv = [for n in migrateSecretNames: { name: secretEnvNames[n], secretRef: n }]

var webName = 'ca-${prefix}-${environmentName}-web'
var webBaseUrl = !empty(publicBaseUrl) ? publicBaseUrl : 'https://${webName}.${appEnvironment.outputs.defaultDomain}'
var publicName = 'ca-${prefix}-${environmentName}-public'
var publicAppUrl = !empty(publicAppBaseUrl) ? publicAppBaseUrl : 'https://${publicName}.${appEnvironment.outputs.defaultDomain}'

var commonEnv = [
  { name: 'ENV', value: environmentName }
  // Migrations run in the migrate job, not at container start.
  { name: 'RUN_MIGRATIONS', value: 'false' }
  { name: 'UVICORN_WORKERS', value: string(uvicornWorkers) }
  // Trust X-Forwarded-* only from the ingress proxies, which run in the
  // Container Apps subnet. Without it the app sees http and drops the
  // HTTPS-only session cookie. Not '*': then uvicorn takes the leftmost
  // X-Forwarded-For entry, which the client controls, as the client address.
  { name: 'FORWARDED_ALLOW_IPS', value: network.outputs.containerAppsSubnetPrefix }
  { name: 'STORAGE_ACCOUNT_NAME', value: storage.outputs.name }
  { name: 'STORAGE_CONTAINER', value: storageContainerName }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: monitoring.outputs.appInsightsConnectionString }
  { name: 'SSO_TENANT_ID', value: ssoTenantId }
  { name: 'SSO_CLIENT_ID', value: ssoClientId }
  { name: 'GRAPH_TENANT_ID', value: graphTenantId }
  { name: 'GRAPH_CLIENT_ID', value: graphClientId }
  { name: 'GRAPH_SHARED_MAILBOX', value: graphSharedMailbox }
  { name: 'SUPER_ADMIN_EMAILS', value: superAdminEmails }
  // Every process that queues mail needs the mode (off queues nothing); the worker sends it.
  { name: 'MAIL_SENDING', value: mailSending }
  // Two addresses: the approval link in an email points at the public app, a Teams card's
  // "Open ticket" points at the web app.
  { name: 'PUBLIC_BASE_URL', value: publicAppUrl }
  { name: 'WEB_BASE_URL', value: webBaseUrl }
  { name: 'TEAMS_NEW_TICKET_ENABLED', value: toLower(string(teamsNewTicketEnabled)) }
  { name: 'TEAMS_NOTIFICATIONS_ENABLED', value: toLower(string(teamsNotificationsEnabled)) }
]

var allowRules = [
  for (cidr, i) in webAllowedCidrs: {
    name: 'allow-${i}'
    description: 'Office or VPN range'
    ipAddressRange: cidr
    action: 'Allow'
  }
]

// Web: agents, login, admin. Restricted to office and VPN ranges.
module web 'modules/container-app.bicep' = if (deployApps) {
  name: 'app-web'
  params: {
    location: location
    tags: tags
    name: webName
    environmentId: appEnvironment.outputs.id
    identityId: identityWeb.id
    registryServer: registry.outputs.loginServer
    image: image
    secrets: webSecrets
    env: concat(commonEnv, webSecretEnv, [
      { name: 'APP_ROLE', value: 'web' }
      { name: 'AZURE_CLIENT_ID', value: identityWeb.properties.clientId }
      { name: 'SSO_REDIRECT_URI', value: '${webBaseUrl}/auth/callback' }
    ])
    ingress: {
      external: true
      targetPort: 8000
      allowInsecure: false
      transport: 'auto'
      ipSecurityRestrictions: allowRules
    }
    cpu: '0.5'
    memory: '1Gi'
    minReplicas: webMinReplicas
    maxReplicas: webMaxReplicas
    httpConcurrentRequests: 30
  }
  dependsOn: [
    keyvault
    postgres
  ]
}

// Public: Graph webhook, feedback and reopen links, Teams bot, /health.
// Same image, different routes (APP_ROLE=public). Open to the internet because
// Microsoft and end users off the VPN have to reach it. Kept warm because Graph
// expects an answer within 3 seconds and a cold start would miss it. It has no
// storage access and cannot read the web session key or the SSO secret.
module publicApp 'modules/container-app.bicep' = if (deployApps && deployPublicApp) {
  name: 'app-public'
  params: {
    location: location
    tags: tags
    name: publicName
    environmentId: appEnvironment.outputs.id
    identityId: identityPublic.id
    registryServer: registry.outputs.loginServer
    image: image
    secrets: publicSecrets
    env: concat(commonEnv, publicSecretEnv, [
      { name: 'APP_ROLE', value: 'public' }
      { name: 'AZURE_CLIENT_ID', value: identityPublic.properties.clientId }
    ])
    ingress: {
      external: true
      targetPort: 8000
      allowInsecure: false
      transport: 'auto'
      ipSecurityRestrictions: []
    }
    cpu: '0.5'
    memory: '1Gi'
    minReplicas: 1
    maxReplicas: 2
    httpConcurrentRequests: 30
  }
  dependsOn: [
    keyvault
    postgres
  ]
}

// Worker: scheduled jobs. No ingress, exactly one replica, otherwise every
// job runs once per replica.
module worker 'modules/container-app.bicep' = if (deployApps && deployWorker) {
  name: 'app-worker'
  params: {
    location: location
    tags: tags
    name: 'ca-${prefix}-${environmentName}-worker'
    environmentId: appEnvironment.outputs.id
    identityId: identityWorker.id
    registryServer: registry.outputs.loginServer
    image: image
    command: ['python', '-m', 'app.worker']
    secrets: workerSecrets
    env: concat(commonEnv, workerSecretEnv, [
      { name: 'APP_ROLE', value: 'worker' }
      { name: 'PRESENCE_ENABLED', value: toLower(string(presenceEnabled)) }
      { name: 'AZURE_CLIENT_ID', value: identityWorker.properties.clientId }
    ])
    ingress: null
    cpu: '0.5'
    memory: '1Gi'
    minReplicas: 1
    maxReplicas: 1
  }
  dependsOn: [
    keyvault
    postgres
  ]
}

// Migrations: started by the deploy workflow before the apps move to the new
// image. Start it with: az containerapp job start -n <job> -g <rg>
module migrate 'modules/migrate-job.bicep' = if (deployApps) {
  name: 'job-migrate'
  params: {
    location: location
    tags: tags
    name: 'job-${prefix}-${environmentName}-migrate'
    environmentId: appEnvironment.outputs.id
    identityId: identityMigrate.id
    registryServer: registry.outputs.loginServer
    image: image
    secrets: migrateSecrets
    env: concat(commonEnv, migrateSecretEnv)
  }
  dependsOn: [
    keyvault
    postgres
  ]
}

output resourceGroupName string = resourceGroup().name
output registryLoginServer string = registry.outputs.loginServer
output registryName string = registry.outputs.name
output keyVaultName string = keyvault.outputs.name
output storageAccountName string = storage.outputs.name
output postgresServerName string = postgres.outputs.name
output containerAppsEnvironmentName string = appEnvironment.outputs.name
output webAppName string = webName
output migrateJobName string = 'job-${prefix}-${environmentName}-migrate'
output webUrl string = webBaseUrl
output publicAppName string = publicName
output publicUrl string = publicAppUrl
output webIdentityClientId string = identityWeb.properties.clientId
