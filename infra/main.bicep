// ---------------------------------------------------------------------------
// VA Loan Concierge — Main Bicep orchestrator
// ---------------------------------------------------------------------------
// Provisions all Azure resources for the demo. Called by `azd provision`.
// Naming convention: {abbreviation}{environmentName}  (e.g. ais-valc-demo-abc)
//
// Next-gen Foundry resource model:
//   Microsoft.CognitiveServices/accounts (kind: AIServices)
//     └── /projects/{name}
//     └── /deployments/{model}
//
// No separate Hub, Azure OpenAI, Storage, or Key Vault resources needed
// for the Foundry layer. Storage is only used by the Function App.
//
// MANUAL STEP (after azd up):
//   Work IQ Calendar connection must be configured in the Foundry portal.
//   Requires M365 Copilot license and interactive OAuth consent — cannot be
//   automated. See README.md "Configure Work IQ Calendar" section.
//   The demo works without it (calendar step is skipped).
// ---------------------------------------------------------------------------

targetScope = 'subscription'

// ── Parameters (prompted by azd) ────────────────────────────────────────────

@minLength(1)
@maxLength(64)
@description('Name of the azd environment (e.g. valc-demo-abc123)')
param environmentName string

@allowed([
  'eastus'
  'eastus2'
  'westus'
  'westus3'
  'swedencentral'
])
@description('Azure region — must support Foundry, AI Search, OpenAI, and Functions')
param location string

@description('GPT model to deploy (e.g. gpt-4.1)')
param modelName string = 'gpt-4.1'

@description('GPT model version')
param modelVersion string = '2025-04-14'

@description('GPT deployment SKU capacity (in thousands of tokens per minute)')
param modelCapacity int = 30

@description('Embedding model for vector search in the knowledge base')
param embeddingModelName string = 'text-embedding-3-small'

@description('Embedding model version')
param embeddingModelVersion string = '1'

@description('Embedding deployment SKU capacity (in thousands of tokens per minute)')
param embeddingCapacity int = 30

@description('Deployment name used by Content Understanding for embeddings')
param cuEmbeddingDeploymentName string = 'cu-embedding'

@description('Primary CU embedding model. If unavailable in-region, deployment falls back automatically.')
param cuEmbeddingPrimaryModelName string = 'text-embedding-3-large'

@description('First CU embedding fallback model')
param cuEmbeddingFallbackModelName string = 'text-embedding-3-small'

@description('Second CU embedding fallback model (manual override option)')
param cuEmbeddingSecondFallbackModelName string = 'text-embedding-ada-002'

@description('CU embedding model version')
param cuEmbeddingModelVersion string = '1'

@description('CU embedding deployment SKU capacity (in thousands of tokens per minute)')
param cuEmbeddingCapacity int = 20

@description('AI Search SKU')
@allowed(['basic', 'standard', 'standard2'])
param searchSku string = 'basic'

@description('Region for Cosmos DB. Defaults to main location. Override with azd env set COSMOS_LOCATION if the primary region has capacity issues (e.g. AZ quota limits).')
param cosmosLocation string = ''

@description('Principal ID of the current user (for RBAC). Populated by azd.')
param principalId string = ''

// ── Resource Group ──────────────────────────────────────────────────────────

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
}

// ── Level 0: Independent foundational resources ─────────────────────────────

module containerRegistry 'modules/container-registry.bicep' = {
  name: 'containerRegistry'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
  }
}

module search 'modules/search.bicep' = {
  name: 'search'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
    sku: searchSku
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
  }
}

module cosmosDb 'modules/cosmos-db.bicep' = {
  name: 'cosmosDb'
  scope: rg
  params: {
    environmentName: environmentName
    location: empty(cosmosLocation) ? location : cosmosLocation
  }
}

// ── Level 1: AI Services account (Foundry + OpenAI + connections) ───────────

module aiServices 'modules/ai-services.bicep' = {
  name: 'aiServices'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
    modelName: modelName
    modelVersion: modelVersion
    modelCapacity: modelCapacity
    embeddingModelName: embeddingModelName
    embeddingModelVersion: embeddingModelVersion
    embeddingCapacity: embeddingCapacity
    cuEmbeddingDeploymentName: cuEmbeddingDeploymentName
    cuEmbeddingPrimaryModelName: cuEmbeddingPrimaryModelName
    cuEmbeddingFallbackModelName: cuEmbeddingFallbackModelName
    cuEmbeddingSecondFallbackModelName: cuEmbeddingSecondFallbackModelName
    cuEmbeddingModelVersion: cuEmbeddingModelVersion
    cuEmbeddingCapacity: cuEmbeddingCapacity
    searchId: search.outputs.searchId
    searchEndpoint: search.outputs.searchEndpoint
  }
}

// ── Level 2: AI Project (child of AI Services) ──────────────────────────────

module aiProject 'modules/ai-project.bicep' = {
  name: 'aiProject'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
    aiServicesName: aiServices.outputs.aiServicesName
  }
}

// ── Level 2: Function App (MCP server — Flex Consumption) ────────────────────

module functionApp 'modules/function-app.bicep' = {
  name: 'functionApp'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    storageAccountName: storage.outputs.storageAccountName
    foundryProjectEndpoint: aiProject.outputs.projectEndpoint
    foundryProjectResourceId: aiProject.outputs.projectId
    advisorSearchEndpoint: search.outputs.searchEndpoint
  }
}

// ── Level 3: RBAC assignments ───────────────────────────────────────────────

module rbac 'modules/rbac.bicep' = {
  name: 'rbac'
  scope: rg
  params: {
    aiServicesPrincipalId: aiServices.outputs.aiServicesPrincipalId
    projectPrincipalId: aiProject.outputs.projectPrincipalId
    searchPrincipalId: search.outputs.searchPrincipalId
    aiServicesId: aiServices.outputs.aiServicesId
    searchId: search.outputs.searchId
    storageAccountId: storage.outputs.storageAccountId
    projectId: aiProject.outputs.projectId
    userPrincipalId: principalId
    functionAppPrincipalId: functionApp.outputs.functionAppPrincipalId
    cosmosAccountId: cosmosDb.outputs.cosmosAccountId
    cosmosAccountName: cosmosDb.outputs.cosmosAccountName
    acrId: containerRegistry.outputs.acrId
  }
}

// ── Outputs (consumed by azd env + hooks) ───────────────────────────────────

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = subscription().tenantId

// Foundry
output FOUNDRY_PROJECT_ENDPOINT string = aiProject.outputs.projectEndpoint
output FOUNDRY_MODEL_DEPLOYMENT string = modelName
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output FOUNDRY_PROJECT_RESOURCE_ID string = aiProject.outputs.projectId

// Embedding model (used by Search indexer for vector search)
output EMBEDDING_MODEL_DEPLOYMENT string = aiServices.outputs.embeddingModelName
output CU_EMBEDDING_MODEL_DEPLOYMENT string = aiServices.outputs.cuEmbeddingDeploymentName
output CU_EMBEDDING_MODEL_FALLBACKS string = aiServices.outputs.cuEmbeddingFallbackModels

// AI Search / Knowledge Base
output ADVISOR_SEARCH_ENDPOINT string = search.outputs.searchEndpoint
output SEARCH_SERVICE_NAME string = search.outputs.searchName

// MCP Function App
output MCP_TOOLS_ENDPOINT string = functionApp.outputs.functionAppUrl
output FUNCTION_APP_NAME string = functionApp.outputs.functionAppName

// Principal IDs (for hooks)
output PROJECT_PRINCIPAL_ID string = aiProject.outputs.projectPrincipalId
output AI_SERVICES_NAME string = aiServices.outputs.aiServicesName
output STORAGE_ACCOUNT_NAME string = storage.outputs.storageAccountName
output STORAGE_ACCOUNT_ENDPOINT string = storage.outputs.storageAccountEndpoint
output KNOWLEDGE_CONTAINER_NAME string = storage.outputs.knowledgeContainerName
output NEWS_CONTAINER_NAME string = storage.outputs.newsContainerName

// Monitoring
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.appInsightsConnectionString

// Cosmos DB (conversation state)
output COSMOS_ENDPOINT string = cosmosDb.outputs.cosmosEndpoint

// Content Understanding (Phase 14)
output AI_SERVICES_ENDPOINT string = aiServices.outputs.aiServicesEndpoint

// Container Registry (hosted agents)
output ACR_NAME string = containerRegistry.outputs.acrName
output ACR_LOGIN_SERVER string = containerRegistry.outputs.acrLoginServer
