// ============================================================================
// Vehicle Dimensions API - Complete Azure Infrastructure
// ============================================================================
// Deploys ALL resources needed for the Vehicle Dimensions API:
//   - Azure AI Services (AI Foundry) with model deployment
//   - Bing Search v7 for grounding
//   - AI Hub + AI Project (Azure AI Foundry)
//   - AI Services connection to the Hub
//   - Container Registry, Container Apps, Log Analytics
//   - RBAC role assignments for managed identity
// ============================================================================

// ──────────────────────────────────────────────
// Parameters
// ──────────────────────────────────────────────

@description('Location for all resources')
param location string = 'swedencentral'

@description('Base name prefix for all resources')
param baseName string = 'vehicleapi'

@description('AI Foundry agent model deployment name (must support tools/function calling)')
param foundryModel string = 'gpt-4.1-mini'

@description('Name for the Bing Grounding connection in AI Foundry')
param bingConnectionName string = 'bing-grounding'

// ──────────────────────────────────────────────
// Variables
// ──────────────────────────────────────────────

var uniqueSuffix = uniqueString(resourceGroup().id)
var aiServicesName = '${baseName}-ai-${uniqueSuffix}'
var storageAccountName = take('${baseName}st${uniqueSuffix}', 24)
var keyVaultName = take('${baseName}kv${uniqueSuffix}', 24)
var aiHubName = '${baseName}-hub-${uniqueSuffix}'
var aiProjectName = '${baseName}-project'
var bingGroundingName = '${baseName}-bing-${uniqueSuffix}'
var logAnalyticsName = '${baseName}-logs-${uniqueSuffix}'
var containerRegistryName = take('${baseName}acr${uniqueSuffix}', 50)
var containerAppEnvName = '${baseName}-env-${uniqueSuffix}'
var containerAppName = '${baseName}-app'

// Well-known role definition IDs
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var keyVaultSecretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// ──────────────────────────────────────────────
// AI Services (Azure AI Foundry)
// ──────────────────────────────────────────────

resource aiServices 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: aiServicesName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: aiServicesName
    publicNetworkAccess: 'Enabled'
  }
}

// Foundry agent model deployment (e.g., gpt-4.1-mini) - used for Bing Grounding
resource foundryModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiServices
  name: foundryModel
  sku: {
    name: 'Standard'
    capacity: 10
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: foundryModel
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}

// ──────────────────────────────────────────────
// Grounding with Bing Search
// ──────────────────────────────────────────────

resource bingGrounding 'Microsoft.Bing/accounts@2020-06-10' = {
  name: bingGroundingName
  location: 'global'
  kind: 'Bing.Grounding'
  sku: {
    name: 'G1'
  }
}

// ──────────────────────────────────────────────
// AI Hub Dependencies (Storage + Key Vault)
// ──────────────────────────────────────────────

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enableRbacAuthorization: true
  }
}

// ──────────────────────────────────────────────
// AI Hub (Azure AI Foundry)
// ──────────────────────────────────────────────

resource aiHub 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: aiHubName
  location: location
  kind: 'Hub'
  sku: {
    name: 'Basic'
    tier: 'Basic'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: 'Vehicle Dimensions API AI Hub'
    description: 'AI Hub for Vehicle Dimensions API'
    storageAccount: storageAccount.id
    keyVault: keyVault.id
    publicNetworkAccess: 'Enabled'
  }
}

// Grant AI Hub access to Storage Account
resource hubStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, aiHub.id, storageBlobDataContributorRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: aiHub.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Grant AI Hub access to Key Vault
resource hubKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, aiHub.id, keyVaultSecretsOfficerRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsOfficerRoleId)
    principalId: aiHub.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ──────────────────────────────────────────────
// AI Hub Connections
// ──────────────────────────────────────────────

// Connect AI Services to the Hub (using AAD auth - local auth is disabled by policy)
resource aiServicesConnection 'Microsoft.MachineLearningServices/workspaces/connections@2024-10-01' = {
  parent: aiHub
  name: '${baseName}-aiservices'
  dependsOn: [hubKeyVaultRole]
  properties: {
    authType: 'AAD'
    category: 'AzureOpenAI'
    isSharedToAll: true
    target: aiServices.properties.endpoint
    metadata: {
      ApiVersion: '2024-10-01'
      ApiType: 'Azure'
      ResourceId: aiServices.id
    }
  }
}

// ──────────────────────────────────────────────
// AI Project
// ──────────────────────────────────────────────

resource aiProject 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: aiProjectName
  location: location
  kind: 'Project'
  sku: {
    name: 'Basic'
    tier: 'Basic'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: 'Vehicle Dimensions API Project'
    description: 'AI Project for Vehicle Dimensions API'
    hubResourceId: aiHub.id
    publicNetworkAccess: 'Enabled'
  }
  dependsOn: [aiServicesConnection]
}

// ──────────────────────────────────────────────
// Container Infrastructure
// ──────────────────────────────────────────────

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: containerRegistryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: true
  }
}

resource containerAppEnvironment 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: containerAppEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ──────────────────────────────────────────────
// Container App (with Managed Identity)
// ──────────────────────────────────────────────

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: containerAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: containerAppEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      // ACR registry config is added after first image push by deploy.ps1
      secrets: []
    }
    template: {
      containers: [
        {
          name: 'vehicle-api'
          // Placeholder image for initial deploy; deploy.ps1 will update with ACR image
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'AZURE_AI_PROJECT_ENDPOINT'
              value: '${aiServices.properties.endpoint}api/projects/${aiProject.name}'
            }
            {
              name: 'AZURE_AI_MODEL_DEPLOYMENT_NAME'
              value: foundryModel
            }
            {
              name: 'BING_CONNECTION_NAME'
              value: bingConnectionName
            }
            {
              name: 'USE_BING_GROUNDING'
              value: 'true'
            }
            {
              name: 'GOV_DATA_AUTO_DOWNLOAD'
              value: 'true'
            }
            {
              name: 'LOG_LEVEL'
              value: 'INFO'
            }
            {
              name: 'BATCH_MAX_CONCURRENT'
              value: '25'
            }
            {
              name: 'BING_MAX_CONCURRENT'
              value: '15'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 3
        rules: [
          {
            name: 'http-rule'
            http: {
              metadata: {
                concurrentRequests: '10'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [foundryModelDeployment, aiProject]
}

// ──────────────────────────────────────────────
// RBAC: Container App → AI Services
// ──────────────────────────────────────────────

resource containerAppCogServicesRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aiServices
  name: guid(aiServices.id, containerApp.id, cognitiveServicesUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// RBAC: Container App → ACR (pull images with managed identity)
resource containerAppAcrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: containerRegistry
  name: guid(containerRegistry.id, containerApp.id, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ──────────────────────────────────────────────
// Outputs
// ──────────────────────────────────────────────

output containerAppUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output containerRegistryLoginServer string = containerRegistry.properties.loginServer
output containerRegistryName string = containerRegistry.name
output aiServicesEndpoint string = aiServices.properties.endpoint
output aiServicesName string = aiServices.name
output aiProjectName string = aiProject.name
output aiProjectEndpoint string = '${aiServices.properties.endpoint}api/projects/${aiProject.name}'
output bingGroundingName string = bingGrounding.name
output aiHubName string = aiHub.name
