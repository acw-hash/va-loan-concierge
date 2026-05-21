// ---------------------------------------------------------------------------
// Azure Container Registry — stores hosted agent container images
// ---------------------------------------------------------------------------

param environmentName string
param location string

// ACR names must be globally unique, alphanumeric, 5-50 chars
var acrName = 'acr${replace(environmentName, '-', '')}'

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

output acrId string = acr.id
output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
