// ---------------------------------------------------------------------------
// AI Services Account (next-gen Foundry resource)
// ---------------------------------------------------------------------------
// Replaces the v1 Hub + separate Azure OpenAI resource.
// This single resource provides: OpenAI models, agent hosting, connections,
// and project management.
//
// Resource type: Microsoft.CognitiveServices/accounts (kind: AIServices)
// ---------------------------------------------------------------------------

param environmentName string
param location string
param modelName string = 'gpt-4.1'
param modelVersion string = '2025-04-14'
param modelCapacity int = 30
param embeddingModelName string = 'text-embedding-3-small'
param embeddingModelVersion string = '1'
param embeddingCapacity int = 30
param miniModelName string = 'gpt-4.1-mini'
param miniModelVersion string = '2025-04-14'
param miniModelCapacity int = 20
param cuEmbeddingDeploymentName string = 'cu-embedding'
param cuEmbeddingPrimaryModelName string = 'text-embedding-3-large'
param cuEmbeddingFallbackModelName string = 'text-embedding-3-small'
param cuEmbeddingSecondFallbackModelName string = 'text-embedding-ada-002'
param cuEmbeddingModelVersion string = '1'
param cuEmbeddingCapacity int = 20
param searchId string
param searchEndpoint string

var unsupportedLargeEmbeddingRegions = [
  'westus'
]
var cuEmbeddingModelName = contains(unsupportedLargeEmbeddingRegions, toLower(location)) ? cuEmbeddingFallbackModelName : cuEmbeddingPrimaryModelName

resource aiServices 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: 'ais-${environmentName}'
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: 'ais-${environmentName}'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
    allowProjectManagement: true
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}

// ── Content filter (custom RAI policy) ──────────────────────────────────────
// Tightens severity thresholds below Microsoft.DefaultV2 and enables jailbreak
// detection, indirect attack detection, and protected material scanning.
// Agent-level guardrails (per-agent, tool call scanning, PII) are created
// separately via REST API in postprovision.ps1.

resource contentFilter 'Microsoft.CognitiveServices/accounts/raiPolicies@2025-04-01-preview' = {
  parent: aiServices
  name: 'va-loan-content-filter'
  properties: {
    mode: 'Default'
    basePolicyName: 'Microsoft.DefaultV2'
    contentFilters: [
      // Prompt filters
      { name: 'Violence', severityThreshold: 'Low', blocking: true, enabled: true, source: 'Prompt' }
      { name: 'Hate', severityThreshold: 'Low', blocking: true, enabled: true, source: 'Prompt' }
      { name: 'Sexual', severityThreshold: 'Medium', blocking: true, enabled: true, source: 'Prompt' }
      { name: 'Selfharm', severityThreshold: 'Low', blocking: true, enabled: true, source: 'Prompt' }
      { name: 'Jailbreak', blocking: true, enabled: true, source: 'Prompt' }
      { name: 'Indirect Attack', blocking: true, enabled: true, source: 'Prompt' }
      { name: 'Profanity', blocking: true, enabled: true, source: 'Prompt' }
      // Completion filters
      { name: 'Violence', severityThreshold: 'Low', blocking: true, enabled: true, source: 'Completion' }
      { name: 'Hate', severityThreshold: 'Low', blocking: true, enabled: true, source: 'Completion' }
      { name: 'Sexual', severityThreshold: 'Medium', blocking: true, enabled: true, source: 'Completion' }
      { name: 'Selfharm', severityThreshold: 'Low', blocking: true, enabled: true, source: 'Completion' }
      { name: 'Protected Material Text', blocking: true, enabled: true, source: 'Completion' }
      { name: 'Protected Material Code', blocking: true, enabled: true, source: 'Completion' }
      { name: 'Profanity', blocking: true, enabled: true, source: 'Completion' }
    ]
  }
}

// ── Model deployment ────────────────────────────────────────────────────────

resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: aiServices
  name: modelName
  sku: {
    name: 'Standard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    raiPolicyName: contentFilter.name
  }
}

// ── Embedding model deployment ─────────────────────────────────────────────

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: aiServices
  name: embeddingModelName
  sku: {
    name: 'Standard'
    capacity: embeddingCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
  }
  dependsOn: [
    modelDeployment
  ]
}

// ── GPT-4.1-mini deployment (required by Content Understanding) ─────────────

resource miniModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: aiServices
  name: miniModelName
  sku: {
    name: 'Standard'
    capacity: miniModelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: miniModelName
      version: miniModelVersion
    }
  }
  dependsOn: [
    embeddingDeployment
  ]
}

// ── Content Understanding embedding deployment ───────────────────────────────
// In regions where text-embedding-3-large is unavailable (for example westus),
// automatically fall back to text-embedding-3-small.

resource cuEmbeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: aiServices
  name: cuEmbeddingDeploymentName
  sku: {
    name: 'Standard'
    capacity: cuEmbeddingCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: cuEmbeddingModelName
      version: cuEmbeddingModelVersion
    }
  }
  dependsOn: [
    miniModelDeployment
  ]
}

// ── AI Search connection (account-level, shared to all projects) ────────────

resource searchConnection 'Microsoft.CognitiveServices/accounts/connections@2025-04-01-preview' = {
  parent: aiServices
  name: 'search-connection'
  properties: {
    category: 'CognitiveSearch'
    authType: 'AAD'
    isSharedToAll: true
    target: searchEndpoint
    metadata: {
      ResourceId: searchId
    }
  }
}

output aiServicesId string = aiServices.id
output aiServicesName string = aiServices.name
output aiServicesPrincipalId string = aiServices.identity.principalId
output aiServicesEndpoint string = 'https://${aiServices.properties.customSubDomainName}.services.ai.azure.com/'
output embeddingModelName string = embeddingModelName
output cuEmbeddingModelName string = cuEmbeddingModelName
output cuEmbeddingDeploymentName string = cuEmbeddingDeploymentName
output cuEmbeddingFallbackModels string = '${cuEmbeddingFallbackModelName},${cuEmbeddingSecondFallbackModelName}'
