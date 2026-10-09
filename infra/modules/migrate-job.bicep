// Manually triggered job that runs `alembic upgrade head` once. The deploy
// workflow starts it and waits for it to finish before moving the apps to the
// new image, so replicas never race to migrate.

param location string
param tags object
param name string
param environmentId string
param identityId string
param registryServer string
param image string
param secrets array
param env array

resource job 'Microsoft.App/jobs@2024-03-01' = {
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
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
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
          name: 'migrate'
          image: image
          command: ['alembic']
          args: ['upgrade', 'head']
          env: env
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
    }
  }
}

output name string = job.name
