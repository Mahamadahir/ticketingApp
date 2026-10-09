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

@description('False for the first deploy: creates the platform (network, database, storage, vault, registry, monitoring) but no apps, because the registry has no image in it yet.')
param deployApps bool = false

@description('Full image reference, e.g. <acr>.azurecr.io/ticketing:<sha>. Required when deployApps is true.')
param image string = ''

@description('Second app that serves only the Graph webhook, feedback/reopen links and /health. Leave false until the app has an APP_ROLE=public mode (plan: phase 3).')
param deployPublicApp bool = false

@description('Background worker. Leave false until the worker entrypoint exists (plan: phase 2).')
param deployWorker bool = false

// ---- Access -----------------------------------------------------------------

@description('Office and VPN ranges allowed to reach the web app, e.g. ["203.0.113.0/24"]. Empty means unrestricted, which is only acceptable while the app is on its generated URL with nothing in it.')
param webAllowedCidrs array = []

@description('Where the web app is reached, e.g. https://tickets.example.org. Empty uses the generated Azure URL.')
param publicBaseUrl string = ''

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
param storageSku string = 'Standard_LRS'

param webMinReplicas int = 1
param webMaxReplicas int = 2
@description('uvicorn processes per replica. Size with the container CPU below.')
param uvicornWorkers int = 2

param logRetentionDays int = 30
@description('Daily ingestion cap for Log Analytics in GB. -1 for no cap.')
param logDailyCapGb int = 1

param purgeProtection bool = false

// ---- Application settings (non-secret) -------------------------------------

param alertEmail string
param ssoTenantId string = ''
param ssoClientId string = ''
param graphTenantId string = ''
param graphClientId string = ''
param graphSharedMailbox string = ''
param superAdminEmails string = ''

// ---- Secrets (supplied at deploy time, stored in Key Vault) -----------------

@secure()
param postgresAdminPassword string
@secure()
param sessionSecretKey string
@secure()
param graphWebhookClientState string
@secure()
@description('Optional until Entra SSO is registered for this environment.')
param ssoClientSecret string = ''
@secure()
@description('Optional until Graph mail is set up (plan: phase 3).')
param graphClientSecret string = ''

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

// One identity shared by every app and the migration job. Created first so its
// role assignments exist before any app tries to pull an image or read a secret.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-${environmentName}'
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
    pullPrincipalId: identity.properties.principalId
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
    accessPrincipalId: identity.properties.principalId
  }
}

module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    tags: tags
    name: keyVaultName
    purgeProtection: purgeProtection
    readerPrincipalId: identity.properties.principalId
    databaseUrl: 'postgresql+psycopg2://${dbAdminLogin}:${uriComponent(postgresAdminPassword)}@${postgres.outputs.fqdn}:5432/${dbName}?sslmode=require'
    sessionSecretKey: sessionSecretKey
    graphWebhookClientState: graphWebhookClientState
    ssoClientSecret: ssoClientSecret
    graphClientSecret: graphClientSecret
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

var secretNames = concat(
  ['database-url', 'session-secret-key', 'graph-webhook-client-state'],
  !empty(ssoClientSecret) ? ['sso-client-secret'] : [],
  !empty(graphClientSecret) ? ['graph-client-secret'] : []
)

// Container Apps read these straight from Key Vault using the shared identity.
// The apps depend on the keyvault and registry modules through the outputs they\n// use, so the identity's role assignments exist before any app starts.
var appSecrets = [
  for n in secretNames: {
    name: n
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/${n}'
    identity: identity.id
  }
]

// database-url -> DATABASE_URL, session-secret-key -> SESSION_SECRET_KEY, ...
var secretEnv = [
  for n in secretNames: {
    name: toUpper(replace(n, '-', '_'))
    secretRef: n
  }
]

var webName = 'ca-${prefix}-${environmentName}-web'
var webBaseUrl = !empty(publicBaseUrl) ? publicBaseUrl : 'https://${webName}.${appEnvironment.outputs.defaultDomain}'

var commonEnv = [
  { name: 'ENV', value: environmentName }
  // Migrations run in the migrate job, not at container start.
  { name: 'RUN_MIGRATIONS', value: 'false' }
  { name: 'UVICORN_WORKERS', value: string(uvicornWorkers) }
  // Lets DefaultAzureCredential pick the shared identity (Blob, Graph later).
  { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
  { name: 'STORAGE_ACCOUNT_NAME', value: storage.outputs.name }
  { name: 'STORAGE_CONTAINER', value: storageContainerName }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: monitoring.outputs.appInsightsConnectionString }
  { name: 'SSO_TENANT_ID', value: ssoTenantId }
  { name: 'SSO_CLIENT_ID', value: ssoClientId }
  { name: 'GRAPH_TENANT_ID', value: graphTenantId }
  { name: 'GRAPH_CLIENT_ID', value: graphClientId }
  { name: 'GRAPH_SHARED_MAILBOX', value: graphSharedMailbox }
  { name: 'SUPER_ADMIN_EMAILS', value: superAdminEmails }
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
    identityId: identity.id
    registryServer: registry.outputs.loginServer
    image: image
    secrets: appSecrets
    env: concat(commonEnv, secretEnv, [
      { name: 'APP_ROLE', value: 'web' }
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
}

// Public: Graph webhook, feedback and reopen links, Teams bot, /health.
// Same image, different routes (APP_ROLE=public). Open to the internet because
// Microsoft and end users off the VPN have to reach it. Kept warm because Graph
// expects an answer within 3 seconds and a cold start would miss it.
module publicApp 'modules/container-app.bicep' = if (deployApps && deployPublicApp) {
  name: 'app-public'
  params: {
    location: location
    tags: tags
    name: 'ca-${prefix}-${environmentName}-public'
    environmentId: appEnvironment.outputs.id
    identityId: identity.id
    registryServer: registry.outputs.loginServer
    image: image
    secrets: appSecrets
    env: concat(commonEnv, secretEnv, [
      { name: 'APP_ROLE', value: 'public' }
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
    identityId: identity.id
    registryServer: registry.outputs.loginServer
    image: image
    command: ['python', '-m', 'app.worker']
    secrets: appSecrets
    env: concat(commonEnv, secretEnv, [
      { name: 'APP_ROLE', value: 'worker' }
    ])
    ingress: null
    cpu: '0.5'
    memory: '1Gi'
    minReplicas: 1
    maxReplicas: 1
  }
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
    identityId: identity.id
    registryServer: registry.outputs.loginServer
    image: image
    secrets: appSecrets
    env: concat(commonEnv, secretEnv)
  }
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
output identityClientId string = identity.properties.clientId
