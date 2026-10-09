// One Container App. Used for web, public and worker; they differ only in
// ingress, replica counts, command and APP_ROLE.

param location string
param tags object
param name string
param environmentId string
param identityId string
param registryServer string
param image string

@description('Overrides the image entrypoint. Empty uses the Dockerfile CMD.')
param command array = []
param secrets array
param env array

@description('Null for no ingress (worker). Otherwise { external, targetPort, allowInsecure, transport, ipSecurityRestrictions }.')
param ingress object?

param cpu string
param memory string
param minReplicas int
param maxReplicas int
@description('Scale out when concurrent requests per replica pass this. 0 for no HTTP scale rule.')
param httpConcurrentRequests int = 0

var hasIngress = ingress != null

// Readiness uses /health, which reports the database, so a database blip
// stops traffic without restarting containers. Liveness only checks the port.
var probes = hasIngress
  ? [
      {
        type: 'Startup'
        httpGet: { path: '/health', port: 8000 }
        periodSeconds: 3
        failureThreshold: 20
      }
      {
        type: 'Readiness'
        httpGet: { path: '/health', port: 8000 }
        periodSeconds: 10
        failureThreshold: 3
      }
      {
        type: 'Liveness'
        tcpSocket: { port: 8000 }
        periodSeconds: 20
        failureThreshold: 3
      }
    ]
  : []

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: hasIngress
        ? {
            external: ingress!.external
            targetPort: ingress!.targetPort
            allowInsecure: ingress!.allowInsecure
            transport: ingress!.transport
            ipSecurityRestrictions: ingress!.ipSecurityRestrictions
          }
        : null
      registries: [
        {
          server: registryServer
          identity: identityId
        }
      ]
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'app'
          image: image
          command: empty(command) ? null : command
          env: env
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          probes: probes
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: httpConcurrentRequests > 0
          ? [
              {
                name: 'http'
                http: {
                  metadata: {
                    concurrentRequests: string(httpConcurrentRequests)
                  }
                }
              }
            ]
          : []
      }
    }
  }
}

output id string = app.id
output name string = app.name
output fqdn string = hasIngress ? app.properties.configuration.ingress.fqdn : ''
