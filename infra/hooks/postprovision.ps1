# ---------------------------------------------------------------------------
# postprovision hook — runs after `azd provision` completes
# ---------------------------------------------------------------------------
# Creates resources that cannot be provisioned via Bicep:
#   1. Upload knowledge docs to blob storage
#   2. Create AI Search data source, index, skillset, and indexer (blob → embeddings → vector index)
#   3. Create RemoteTool project connections (KB MCP + custom MCP)
#   4. Create Foundry guardrail policies (per-agent safety controls)
#   5. Deploy MCP server Function App
#   6. Write .env from azd env values
#   7. Register Foundry agents
#
# NOTE: Foundry IQ Knowledge Base creation is a MANUAL step after deployment.
#       See README.md for instructions.
# ---------------------------------------------------------------------------

$ErrorActionPreference = "Stop"

Write-Host "=== postprovision: Setting up knowledge base and Foundry connections ==="

# -- Load outputs from azd env --

$PROJECT_RESOURCE_ID = (azd env get-value FOUNDRY_PROJECT_RESOURCE_ID 2>$null)
$SEARCH_ENDPOINT = (azd env get-value ADVISOR_SEARCH_ENDPOINT 2>$null)
$SEARCH_SERVICE_NAME = (azd env get-value SEARCH_SERVICE_NAME 2>$null)
$MCP_TOOLS_ENDPOINT = (azd env get-value MCP_TOOLS_ENDPOINT 2>$null)
$STORAGE_ACCOUNT_NAME = (azd env get-value STORAGE_ACCOUNT_NAME 2>$null)
$AI_SERVICES_NAME = (azd env get-value AI_SERVICES_NAME 2>$null)
$EMBEDDING_MODEL = (azd env get-value EMBEDDING_MODEL_DEPLOYMENT 2>$null)
if (-not $EMBEDDING_MODEL) { $EMBEDDING_MODEL = "text-embedding-3-small" }
$CU_EMBEDDING_MODEL = (azd env get-value CU_EMBEDDING_MODEL_DEPLOYMENT 2>$null)
if (-not $CU_EMBEDDING_MODEL) { $CU_EMBEDDING_MODEL = $EMBEDDING_MODEL }
$CU_EMBEDDING_FALLBACKS = (azd env get-value CU_EMBEDDING_MODEL_FALLBACKS 2>$null)
if (-not $CU_EMBEDDING_FALLBACKS) { $CU_EMBEDDING_FALLBACKS = "text-embedding-3-small,text-embedding-ada-002" }
$AZURE_RESOURCE_GROUP = (azd env get-value AZURE_RESOURCE_GROUP 2>$null)
$KNOWLEDGE_CONTAINER = (azd env get-value KNOWLEDGE_CONTAINER_NAME 2>$null)
if (-not $KNOWLEDGE_CONTAINER) { $KNOWLEDGE_CONTAINER = "loan-guidelines" }

if (-not $PROJECT_RESOURCE_ID) {
    Write-Error "FOUNDRY_PROJECT_RESOURCE_ID not set. Did azd provision complete?"
    exit 1
}

# -- Get access tokens --

$TOKEN = (az account get-access-token --query accessToken -o tsv)
$SEARCH_TOKEN = (az account get-access-token --resource https://search.azure.com --query accessToken -o tsv)
$STORAGE_TOKEN = (az account get-access-token --resource https://storage.azure.com/ --query accessToken -o tsv)

# -- 1. Upload knowledge documents to blob storage --

$STORAGE_URL = "https://${STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
Write-Host "Uploading knowledge documents to ${STORAGE_URL}/${KNOWLEDGE_CONTAINER}/"

$docs = @(
    "knowledge/va_guidelines.md",
    "knowledge/lender_products.md",
    "knowledge/loan_process_faq.md",
    "knowledge/va_funding_fee_tables.md",
    "knowledge/va_entitlement_calculations.md",
    "knowledge/va_minimum_property_requirements.md",
    "knowledge/va_appraisal_and_tidewater.md",
    "knowledge/va_coe_and_eligibility_documentation.md",
    "knowledge/va_closing_costs_and_allowable_fees.md",
    "knowledge/va_jumbo_and_renovation_loans.md",
    "knowledge/va_state_overlays_and_lender_guidelines.md"
)
foreach ($doc in $docs) {
    if (-not (Test-Path $doc)) {
        Write-Host "  WARNING: $doc not found, skipping"
        continue
    }
    $BLOB_NAME = Split-Path $doc -Leaf
    $headers = @{
        "Authorization"    = "Bearer $STORAGE_TOKEN"
        "x-ms-blob-type"   = "BlockBlob"
        "Content-Type"     = "text/markdown"
        "x-ms-version"     = "2023-11-03"
    }
    $body = [System.IO.File]::ReadAllBytes((Resolve-Path $doc))
    try {
        $resp = Invoke-WebRequest -Uri "${STORAGE_URL}/${KNOWLEDGE_CONTAINER}/${BLOB_NAME}" `
            -Method PUT -Headers $headers -Body $body -UseBasicParsing
        Write-Host "  ${BLOB_NAME}: HTTP $($resp.StatusCode)"
    } catch {
        Write-Host "  ${BLOB_NAME}: HTTP $($_.Exception.Response.StatusCode.value__) - $($_.ErrorDetails.Message)"
    }
}

# -- 2. Create AI Search data source (points at blob container) --

$KB_NAME = "kb-va-loan-concierge"
$DATASOURCE_NAME = "${KB_NAME}-datasource"
Write-Host "Creating search data source: $DATASOURCE_NAME"

$SUBSCRIPTION_ID = (azd env get-value AZURE_SUBSCRIPTION_ID)
$RESOURCE_ID = "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${AZURE_RESOURCE_GROUP}/providers/Microsoft.Storage/storageAccounts/${STORAGE_ACCOUNT_NAME}"

$datasourceBody = @{
    name = $DATASOURCE_NAME
    type = "azureblob"
    credentials = @{
        connectionString = "ResourceId=${RESOURCE_ID};"
    }
    container = @{
        name = $KNOWLEDGE_CONTAINER
    }
} | ConvertTo-Json -Depth 5

$searchHeaders = @{
    "Authorization" = "Bearer $SEARCH_TOKEN"
    "Content-Type"  = "application/json"
}

try {
    $resp = Invoke-WebRequest -Uri "${SEARCH_ENDPOINT}/datasources/${DATASOURCE_NAME}?api-version=2024-11-01-preview" `
        -Method PUT -Headers $searchHeaders -Body $datasourceBody -UseBasicParsing
    Write-Host "  Data source: HTTP $($resp.StatusCode)"
} catch {
    Write-Host "  Data source: HTTP $($_.Exception.Response.StatusCode.value__) - $($_.ErrorDetails.Message)"
}

# -- 3. Create AI Search index (with vector field for hybrid search) --

Write-Host "Creating search index: $KB_NAME"

$indexBody = @{
    name = $KB_NAME
    fields = @(
        @{ name = "id"; type = "Edm.String"; key = $true; filterable = $true }
        @{ name = "content"; type = "Edm.String"; searchable = $true; analyzer = "standard.lucene" }
        @{ name = "content_vector"; type = "Collection(Edm.Single)"; searchable = $true; dimensions = 1536; vectorSearchProfile = "default-profile" }
        @{ name = "metadata_storage_name"; type = "Edm.String"; filterable = $true; facetable = $true }
        @{ name = "metadata_storage_path"; type = "Edm.String"; filterable = $true }
    )
    vectorSearch = @{
        algorithms = @(
            @{
                name = "default-algorithm"
                kind = "hnsw"
                hnswParameters = @{
                    metric = "cosine"
                    m = 4
                    efConstruction = 400
                    efSearch = 500
                }
            }
        )
        profiles = @(
            @{
                name = "default-profile"
                algorithm = "default-algorithm"
            }
        )
    }
    semantic = @{
        defaultConfiguration = "default"
        configurations = @(
            @{
                name = "default"
                prioritizedFields = @{
                    prioritizedContentFields = @(@{ fieldName = "content" })
                    titleField = @{ fieldName = "metadata_storage_name" }
                }
            }
        )
    }
} | ConvertTo-Json -Depth 10

try {
    $resp = Invoke-WebRequest -Uri "${SEARCH_ENDPOINT}/indexes/${KB_NAME}?api-version=2024-11-01-preview" `
        -Method PUT -Headers $searchHeaders -Body $indexBody -UseBasicParsing
    Write-Host "  Index: HTTP $($resp.StatusCode)"
} catch {
    Write-Host "  Index: HTTP $($_.Exception.Response.StatusCode.value__) - $($_.ErrorDetails.Message)"
}

# -- 4. Create AI Search skillset (embedding generation) --

$SKILLSET_NAME = "${KB_NAME}-skillset"
Write-Host "Creating search skillset: $SKILLSET_NAME (embedding model: $EMBEDDING_MODEL)"

$skillsetBody = @{
    name = $SKILLSET_NAME
    skills = @(
        @{
            "@odata.type" = "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill"
            name = "content-embedding"
            description = "Generate embeddings for knowledge base content"
            context = "/document"
            modelName = $EMBEDDING_MODEL
            resourceUri = "https://${AI_SERVICES_NAME}.openai.azure.com"
            deploymentId = $EMBEDDING_MODEL
            inputs = @(
                @{ name = "text"; source = "/document/content" }
            )
            outputs = @(
                @{ name = "embedding"; targetName = "content_vector" }
            )
        }
    )
} | ConvertTo-Json -Depth 10

try {
    $resp = Invoke-WebRequest -Uri "${SEARCH_ENDPOINT}/skillsets/${SKILLSET_NAME}?api-version=2024-11-01-preview" `
        -Method PUT -Headers $searchHeaders -Body $skillsetBody -UseBasicParsing
    Write-Host "  Skillset: HTTP $($resp.StatusCode)"
} catch {
    Write-Host "  Skillset: HTTP $($_.Exception.Response.StatusCode.value__) - $($_.ErrorDetails.Message)"
}

# -- 5. Create AI Search indexer (blob -> skillset -> index) --

$INDEXER_NAME = "${KB_NAME}-indexer"
Write-Host "Creating search indexer: $INDEXER_NAME"

$indexerBody = @{
    name = $INDEXER_NAME
    dataSourceName = $DATASOURCE_NAME
    skillsetName = $SKILLSET_NAME
    targetIndexName = $KB_NAME
    parameters = @{
        configuration = @{
            parsingMode = "text"
            dataToExtract = "contentAndMetadata"
        }
    }
    fieldMappings = @(
        @{ sourceFieldName = "metadata_storage_path"; targetFieldName = "id"; mappingFunction = @{ name = "base64Encode" } }
        @{ sourceFieldName = "metadata_storage_name"; targetFieldName = "metadata_storage_name" }
    )
    outputFieldMappings = @(
        @{ sourceFieldName = "/document/content_vector"; targetFieldName = "content_vector" }
    )
} | ConvertTo-Json -Depth 10

try {
    $resp = Invoke-WebRequest -Uri "${SEARCH_ENDPOINT}/indexers/${INDEXER_NAME}?api-version=2024-11-01-preview" `
        -Method PUT -Headers $searchHeaders -Body $indexerBody -UseBasicParsing
    Write-Host "  Indexer: HTTP $($resp.StatusCode)"
} catch {
    Write-Host "  Indexer: HTTP $($_.Exception.Response.StatusCode.value__) - $($_.ErrorDetails.Message)"
}

# Run the indexer immediately
Write-Host "Running indexer..."
try {
    $resp = Invoke-WebRequest -Uri "${SEARCH_ENDPOINT}/indexers/${INDEXER_NAME}/run?api-version=2024-11-01-preview" `
        -Method POST -Headers $searchHeaders -Body "" -UseBasicParsing
    Write-Host "  Indexer run: HTTP $($resp.StatusCode)"
} catch {
    Write-Host "  Indexer run: HTTP $($_.Exception.Response.StatusCode.value__) - $($_.ErrorDetails.Message)"
}

# -- 6. Create RemoteTool connection for KB MCP --

$ADVISOR_MCP_CONNECTION = "kb-va-loan-demo-mcp"
$KB_MCP_URL = "${SEARCH_ENDPOINT}/knowledgebases/${KB_NAME}/mcp?api-version=2025-11-01-preview"
Write-Host "Creating RemoteTool connection: $ADVISOR_MCP_CONNECTION"

$connKbBody = @{
    properties = @{
        category = "RemoteTool"
        authType = "ProjectManagedIdentity"
        target = $KB_MCP_URL
        isSharedToAll = $true
        audience = "https://search.azure.com/"
        metadata = @{ ApiType = "Azure" }
    }
} | ConvertTo-Json -Depth 5

$armHeaders = @{
    "Authorization" = "Bearer $TOKEN"
    "Content-Type"  = "application/json"
}

try {
    $resp = Invoke-WebRequest -Uri "https://management.azure.com${PROJECT_RESOURCE_ID}/connections/${ADVISOR_MCP_CONNECTION}?api-version=2025-04-01-preview" `
        -Method PUT -Headers $armHeaders -Body $connKbBody -UseBasicParsing
    Write-Host "  KB MCP connection: HTTP $($resp.StatusCode)"
} catch {
    Write-Host "  KB MCP connection: HTTP $($_.Exception.Response.StatusCode.value__) - $($_.ErrorDetails.Message)"
}

# -- 7. Create RemoteTool connection for custom MCP server --

$MCP_TOOLS_CONNECTION = "va-loan-action-mcp-conn"
Write-Host "Creating RemoteTool connection: $MCP_TOOLS_CONNECTION"

$connMcpBody = @{
    properties = @{
        category = "RemoteTool"
        authType = "None"
        target = $MCP_TOOLS_ENDPOINT
    }
} | ConvertTo-Json -Depth 5

try {
    $resp = Invoke-WebRequest -Uri "https://management.azure.com${PROJECT_RESOURCE_ID}/connections/${MCP_TOOLS_CONNECTION}?api-version=2025-04-01-preview" `
        -Method PUT -Headers $armHeaders -Body $connMcpBody -UseBasicParsing
    Write-Host "  MCP tools connection: HTTP $($resp.StatusCode)"
} catch {
    Write-Host "  MCP tools connection: HTTP $($_.Exception.Response.StatusCode.value__) - $($_.ErrorDetails.Message)"
}

# -- 8. Create Foundry Guardrails (per-agent safety policies) --
# Two custom raiPolicy resources: one for advisor, one for calculator+scheduler.
# Idempotent PUT — safe to rerun. Assignment to agents is a manual portal step.

$GUARDRAIL_API_VERSION = "2025-04-01-preview"
$GUARDRAIL_BASE_URI = "https://management.azure.com/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${AZURE_RESOURCE_GROUP}/providers/Microsoft.CognitiveServices/accounts/${AI_SERVICES_NAME}"

Write-Host ""
Write-Host "=== Creating Foundry guardrail policies ==="

# 8a. Advisor Guardrail (user input + output)
Write-Host "Creating guardrail: va-loan-advisor-guardrail"

$advisorGuardrail = @{
    properties = @{
        mode = "Default"
        basePolicyName = "Microsoft.DefaultV2"
        contentFilters = @(
            # Prompt filters
            @{ name = "Violence"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Hate"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Sexual"; severityThreshold = "Medium"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Selfharm"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Jailbreak"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Indirect Attack"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Profanity"; blocking = $true; enabled = $true; source = "Prompt" }
            # Completion filters
            @{ name = "Violence"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Completion" }
            @{ name = "Hate"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Completion" }
            @{ name = "Sexual"; severityThreshold = "Medium"; blocking = $true; enabled = $true; source = "Completion" }
            @{ name = "Selfharm"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Completion" }
            @{ name = "Protected Material Text"; blocking = $true; enabled = $true; source = "Completion" }
            @{ name = "Protected Material Code"; blocking = $true; enabled = $true; source = "Completion" }
            @{ name = "Profanity"; blocking = $true; enabled = $true; source = "Completion" }
        )
    }
} | ConvertTo-Json -Depth 10

try {
    $resp = Invoke-WebRequest -Uri "${GUARDRAIL_BASE_URI}/raiPolicies/va-loan-advisor-guardrail?api-version=${GUARDRAIL_API_VERSION}" `
        -Method PUT -Headers $armHeaders -Body $advisorGuardrail -UseBasicParsing
    Write-Host "  Advisor guardrail: HTTP $($resp.StatusCode)"
} catch {
    Write-Host "  Advisor guardrail: HTTP $($_.Exception.Response.StatusCode.value__) - $($_.ErrorDetails.Message)"
}

# 8b. Tools Guardrail (user input + tool call + tool response + output)
Write-Host "Creating guardrail: va-loan-tools-guardrail"

$toolsGuardrail = @{
    properties = @{
        mode = "Default"
        basePolicyName = "Microsoft.DefaultV2"
        contentFilters = @(
            # Prompt filters
            @{ name = "Violence"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Hate"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Sexual"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Selfharm"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Jailbreak"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Indirect Attack"; blocking = $true; enabled = $true; source = "Prompt" }
            @{ name = "Profanity"; blocking = $true; enabled = $true; source = "Prompt" }
            # Completion filters
            @{ name = "Violence"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Completion" }
            @{ name = "Hate"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Completion" }
            @{ name = "Sexual"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Completion" }
            @{ name = "Selfharm"; severityThreshold = "Low"; blocking = $true; enabled = $true; source = "Completion" }
            @{ name = "Protected Material Text"; blocking = $true; enabled = $true; source = "Completion" }
            @{ name = "Profanity"; blocking = $true; enabled = $true; source = "Completion" }
        )
    }
} | ConvertTo-Json -Depth 10

try {
    $resp = Invoke-WebRequest -Uri "${GUARDRAIL_BASE_URI}/raiPolicies/va-loan-tools-guardrail?api-version=${GUARDRAIL_API_VERSION}" `
        -Method PUT -Headers $armHeaders -Body $toolsGuardrail -UseBasicParsing
    Write-Host "  Tools guardrail: HTTP $($resp.StatusCode)"
} catch {
    Write-Host "  Tools guardrail: HTTP $($_.Exception.Response.StatusCode.value__) - $($_.ErrorDetails.Message)"
}

# -- 9. Save connection names to azd env for use by app --

azd env set ADVISOR_KNOWLEDGE_BASE_NAME $KB_NAME
azd env set ADVISOR_MCP_CONNECTION $ADVISOR_MCP_CONNECTION
azd env set MCP_TOOLS_CONNECTION $MCP_TOOLS_CONNECTION

# Hosted rate-intelligence agent fallback auth: set Search query key in azd env
# so agent.yaml can inject RATE_SEARCH_API_KEY at deploy time.
$RATE_SEARCH_API_KEY = ""
if ($SEARCH_SERVICE_NAME -and $AZURE_RESOURCE_GROUP) {
    try {
        $RATE_SEARCH_API_KEY = (az search query-key list `
            --service-name $SEARCH_SERVICE_NAME `
            --resource-group $AZURE_RESOURCE_GROUP `
            --query "[0].key" -o tsv 2>$null)
    } catch {
        $RATE_SEARCH_API_KEY = ""
    }
}

if ($RATE_SEARCH_API_KEY) {
    azd env set RATE_SEARCH_API_KEY $RATE_SEARCH_API_KEY
    Write-Host "  RATE_SEARCH_API_KEY set in azd env"
} else {
    Write-Host "  WARNING: Could not resolve RATE_SEARCH_API_KEY from Search query keys"
}

# -- 10. Deploy MCP server Function App --
# azd service deploy uses storage account keys which are blocked by policy.
# Deploy via az CLI instead (uses Azure AD auth).

$FUNC_APP_NAME = (azd env get-value FUNCTION_APP_NAME 2>$null)

if ($FUNC_APP_NAME -and $AZURE_RESOURCE_GROUP) {
    Write-Host ""
    Write-Host "=== Deploying MCP server to $FUNC_APP_NAME ==="

    # Sync the content ingestion pipeline files from the repo root into the
    # mcp-server/tools/ package before publishing.  The Function App cannot
    # import from parent directories, so these files must live inside mcp-server/.
    # This copy step is the single source of truth — edit tools/ at the root,
    # never edit mcp-server/tools/ directly.
    Write-Host "  Syncing tools/content_ingestion.py → mcp-server/tools/"
    New-Item -ItemType Directory -Force -Path "mcp-server/tools" | Out-Null
    Copy-Item "tools/content_ingestion.py" "mcp-server/tools/content_ingestion.py" -Force
    Copy-Item "tools/feed_sources.json"    "mcp-server/tools/feed_sources.json"    -Force
    if (-not (Test-Path "mcp-server/tools/__init__.py")) {
        "" | Out-File "mcp-server/tools/__init__.py" -Encoding utf8
    }

    Write-Host "  Syncing agents/newsletter_agent.py → mcp-server/"
    Copy-Item "agents/newsletter_agent.py" "mcp-server/newsletter_agent.py" -Force

    # Set app settings that are not wired via Bicep (hook-managed values).
    # FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_PROJECT_RESOURCE_ID, and ADVISOR_SEARCH_ENDPOINT
    # are set by Bicep. ADVISOR_KNOWLEDGE_BASE_NAME and ADVISOR_MCP_CONNECTION are
    # fixed strings owned by this hook — set them here so the newsletter trigger can
    # call resolve_version() without missing env vars.
    Write-Host "  Setting newsletter env vars on Function App..."
    az functionapp config appsettings set `
        --name $FUNC_APP_NAME `
        --resource-group $AZURE_RESOURCE_GROUP `
        --settings `
            "ADVISOR_KNOWLEDGE_BASE_NAME=$KB_NAME" `
            "ADVISOR_MCP_CONNECTION=$ADVISOR_MCP_CONNECTION" `
        --output none

    Push-Location mcp-server
    func azure functionapp publish $FUNC_APP_NAME --python
    Pop-Location
    Write-Host "  MCP server deployed successfully"
} else {
    Write-Host "  WARNING: FUNCTION_APP_NAME or AZURE_RESOURCE_GROUP not set, skipping MCP deploy"
}

# -- 11. Write .env from azd env values (preserve manual entries) --
# Strategy: azd-managed keys are always written from azd outputs.
# Any key already in .env that is NOT in the azd-managed set is preserved.
# This protects manually-configured values (e.g. SCHEDULER_CALENDAR_*).

Write-Host ""
Write-Host "=== Writing .env from azd environment (preserving manual entries) ==="

$ENV_FILE = ".env"

# Keys that azd authoritatively owns — always written from azd outputs.
$azdValues = [ordered]@{
    "FOUNDRY_PROJECT_ENDPOINT"              = (azd env get-value FOUNDRY_PROJECT_ENDPOINT 2>$null)
    "FOUNDRY_MODEL_DEPLOYMENT"              = (azd env get-value FOUNDRY_MODEL_DEPLOYMENT 2>$null)
    "AZURE_SUBSCRIPTION_ID"                 = (azd env get-value AZURE_SUBSCRIPTION_ID 2>$null)
    "FOUNDRY_PROJECT_RESOURCE_ID"           = (azd env get-value FOUNDRY_PROJECT_RESOURCE_ID 2>$null)
    "ADVISOR_KNOWLEDGE_BASE_NAME"           = $KB_NAME
    "ADVISOR_SEARCH_ENDPOINT"               = $SEARCH_ENDPOINT
    "RATE_SEARCH_API_KEY"                   = (azd env get-value RATE_SEARCH_API_KEY 2>$null)
    "ADVISOR_MCP_CONNECTION"                = $ADVISOR_MCP_CONNECTION
    "MCP_TOOLS_ENDPOINT"                    = $MCP_TOOLS_ENDPOINT
    "MCP_TOOLS_CONNECTION"                  = $MCP_TOOLS_CONNECTION
    "AI_SERVICES_NAME"                      = $AI_SERVICES_NAME
    "EMBEDDING_MODEL_DEPLOYMENT"            = $EMBEDDING_MODEL
    "APPLICATIONINSIGHTS_CONNECTION_STRING"  = (azd env get-value APPLICATIONINSIGHTS_CONNECTION_STRING 2>$null)
    "COSMOS_ENDPOINT"                       = (azd env get-value COSMOS_ENDPOINT 2>$null)
    # ── Content Understanding (Phase 14) ─────────────────────────────────────
    # CU_ENDPOINT uses the services.ai.azure.com format required by the CU SDK.
    # CU_COMPLETION_DEPLOYMENT mirrors FOUNDRY_MODEL_DEPLOYMENT by default, but is
    # kept as a separate variable — CU may need a different model than the agent pipeline.
    # CU output goes to blob storage (news-articles container); Foundry IQ handles
    # vectorization automatically when the container is added as a KB source.
    "CU_ENDPOINT"                           = (azd env get-value AI_SERVICES_ENDPOINT 2>$null)
    "CU_COMPLETION_DEPLOYMENT"              = (azd env get-value FOUNDRY_MODEL_DEPLOYMENT 2>$null)
    "CU_MINI_MODEL_DEPLOYMENT"              = "gpt-4.1-mini"
    "CU_LARGE_EMBEDDING_DEPLOYMENT"         = $CU_EMBEDDING_MODEL
    "CU_EMBEDDING_MODEL_FALLBACKS"          = $CU_EMBEDDING_FALLBACKS
    "CU_ANALYZER_NAME"                      = "vaMortgageNews"
    "CU_NEWS_BLOB_CONTAINER"                = "news-articles"
    "STORAGE_ACCOUNT_ENDPOINT"              = (azd env get-value STORAGE_ACCOUNT_ENDPOINT 2>$null)
}

# SCHEDULER_CALENDAR_ENDPOINT and SCHEDULER_CALENDAR_CONNECTION are manual-only
# (configured in the Foundry portal). They are never resolved from azd env.
# The merge logic below preserves them from the existing .env if present.

# Read existing .env to preserve manually-set keys.
$existingValues = [ordered]@{}
if (Test-Path $ENV_FILE) {
    foreach ($line in (Get-Content $ENV_FILE)) {
        if ($line -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$") {
            $existingValues[$Matches[1]] = $Matches[2]
        }
    }
}

# Merge: azd values take precedence for azd-managed keys.
# Existing values are preserved for all other keys.
$merged = [ordered]@{}

# Start with azd-managed values.
foreach ($key in $azdValues.Keys) {
    $merged[$key] = $azdValues[$key]
}

# Add any existing keys that azd doesn't manage (manual entries).
$preservedCount = 0
foreach ($key in $existingValues.Keys) {
    if (-not $merged.Contains($key)) {
        $merged[$key] = $existingValues[$key]
        $preservedCount++
    }
}

# Write the merged result.
$lines = @()
foreach ($key in $merged.Keys) {
    $lines += "${key}=$($merged[$key])"
}
Set-Content -Path $ENV_FILE -Value ($lines -join "`n") -NoNewline

if ($preservedCount -gt 0) {
    Write-Host "  .env written ($preservedCount manual entries preserved)"
} else {
    Write-Host "  .env written"
}

# -- 12. Register Foundry agents --

Write-Host ""
Write-Host "=== Registering Foundry agents ==="
python deploy_workflow.py

Write-Host ""
Write-Host "=== postprovision complete ==="
Write-Host ""
Write-Host "  MANUAL STEPS REQUIRED (see README.md for details):"
Write-Host ""
Write-Host "  1. Create Foundry IQ Knowledge Base in the Foundry portal"
Write-Host "     (wraps the search index that was just created)"
Write-Host ""
Write-Host "  2. Assign guardrails to agents in the Foundry portal:"
Write-Host "     Build > Agents > va-loan-advisor-iq > Guardrails > Manage"
Write-Host "       -> Assign 'va-loan-advisor-guardrail'"
Write-Host "     Build > Agents > va-loan-calculator-mcp > Guardrails > Manage"
Write-Host "       -> Assign 'va-loan-tools-guardrail'"
Write-Host "     Build > Agents > va-loan-scheduler-mcp > Guardrails > Manage"
Write-Host "       -> Assign 'va-loan-tools-guardrail'"
Write-Host ""
Write-Host "  3. (Optional) Configure Work IQ Calendar connection"
Write-Host "     for M365 calendar integration"
Write-Host ""
Write-Host "  After completing manual steps, run locally:"
Write-Host "    uvicorn api.server:app --reload --port 8000"
Write-Host "    cd ui && npm run dev"
Write-Host ""
