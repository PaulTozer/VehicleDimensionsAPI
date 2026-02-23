<# 
.SYNOPSIS
    Deploys the Vehicle Dimensions API and ALL supporting infrastructure to Azure.

.DESCRIPTION
    This script provisions the complete Azure environment from scratch:
      1. Azure AI Services (AI Foundry) with model deployment
      2. AI Hub + AI Project (Azure AI Foundry)
      4. AI Services connection to the Hub
      5. Bing Grounding connection to the Hub
      6. Azure Container Registry
      7. Azure Container Apps (with managed identity + RBAC)
      8. Builds and pushes the Docker image
      9. Updates the Container App with the new image

.PARAMETER ResourceGroupName
    Name of the Azure resource group to create/use (default: rg-vehicle-api-swedencentral)

.PARAMETER Location
    Azure region (default: swedencentral)

.PARAMETER BaseName
    Base name prefix for all resources (default: vehicleapi)

.PARAMETER FoundryModel
    Model for Bing Grounding agent - must support function calling (default: gpt-4.1-mini)

.PARAMETER BingConnectionName
    Name for the Bing Grounding connection in AI Foundry (default: bing-grounding)

.PARAMETER SkipInfrastructure
    Skip infrastructure deployment - only rebuild and push the Docker image

.EXAMPLE
    # Deploy everything with defaults
    .\deploy.ps1

.EXAMPLE
    # Redeploy just the app (skip infra)
    .\deploy.ps1 -SkipInfrastructure
#>

param(
    [Parameter(Mandatory=$false)]
    [string]$ResourceGroupName = "rg-vehicle-api-swedencentral",
    
    [Parameter(Mandatory=$false)]
    [string]$Location = "swedencentral",

    [Parameter(Mandatory=$false)]
    [string]$BaseName = "vehicleapi",

    [Parameter(Mandatory=$false)]
    [string]$FoundryModel = "gpt-4.1-mini",

    [Parameter(Mandatory=$false)]
    [string]$BingConnectionName = "bing-grounding",

    [Parameter(Mandatory=$false)]
    [switch]$SkipInfrastructure
)

$ErrorActionPreference = "Stop"

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "Vehicle Dimensions API - Azure Infrastructure Deploy" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host ""

# ─────────────────────────────────────────────────
# Step 1: Pre-flight checks
# ─────────────────────────────────────────────────

Write-Host "Step 1: Pre-flight checks..." -ForegroundColor Yellow

# Check Azure CLI is logged in
$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) {
    Write-Host "ERROR: Not logged in. Run 'az login' first." -ForegroundColor Red
    exit 1
}
Write-Host "  Logged in as: $($account.user.name)" -ForegroundColor Green
Write-Host "  Subscription: $($account.name) ($($account.id))" -ForegroundColor Green

# Check required CLI extensions
$extensions = @("containerapp")
foreach ($ext in $extensions) {
    $installed = az extension show --name $ext 2>$null
    if (-not $installed) {
        Write-Host "  Installing Azure CLI extension: $ext..." -ForegroundColor Gray
        az extension add --name $ext --yes --output none
    }
}
Write-Host "  Azure CLI extensions: OK" -ForegroundColor Green

# Check Bicep availability
$bicepVersion = az bicep version 2>$null
if (-not $bicepVersion) {
    Write-Host "  Installing Bicep CLI..." -ForegroundColor Gray
    az bicep install --output none
}
Write-Host "  Bicep CLI: OK" -ForegroundColor Green

# Check that the Bing resource provider is registered
Write-Host "  Checking resource provider registrations..." -ForegroundColor Gray
$bingProvider = az provider show --namespace Microsoft.Bing --query "registrationState" -o tsv 2>$null
if ($bingProvider -ne "Registered") {
    Write-Host "  Registering Microsoft.Bing provider (may take a few minutes)..." -ForegroundColor Gray
    az provider register --namespace Microsoft.Bing --output none
    $retries = 0
    while ($retries -lt 30) {
        Start-Sleep -Seconds 10
        $bingProvider = az provider show --namespace Microsoft.Bing --query "registrationState" -o tsv 2>$null
        if ($bingProvider -eq "Registered") { break }
        $retries++
        Write-Host "    Waiting for registration... ($retries/30)" -ForegroundColor Gray
    }
    if ($bingProvider -ne "Registered") {
        Write-Host "WARNING: Microsoft.Bing provider not yet registered. Bing Search deployment may fail." -ForegroundColor Yellow
    }
}
Write-Host "  Resource providers: OK" -ForegroundColor Green
Write-Host ""

# ─────────────────────────────────────────────────
# Step 2: Create Resource Group
# ─────────────────────────────────────────────────

Write-Host "Step 2: Creating resource group '$ResourceGroupName' in $Location..." -ForegroundColor Yellow
az group create --name $ResourceGroupName --location $Location --output none
Write-Host "  Resource group: OK" -ForegroundColor Green
Write-Host ""

# ─────────────────────────────────────────────────
# Step 3: Deploy Infrastructure (Bicep)
# ─────────────────────────────────────────────────

if (-not $SkipInfrastructure) {
    Write-Host "Step 3: Deploying ALL infrastructure via Bicep..." -ForegroundColor Yellow
    Write-Host "  This creates:" -ForegroundColor Gray
    Write-Host "    - Azure AI Services (AI Foundry)" -ForegroundColor Gray
    Write-Host "    - Model deployment: $FoundryModel" -ForegroundColor Gray
    Write-Host "    - Grounding with Bing Search (G1)" -ForegroundColor Gray
    Write-Host "    - AI Hub + AI Project" -ForegroundColor Gray
    Write-Host "    - Storage Account + Key Vault (for Hub)" -ForegroundColor Gray
    Write-Host "    - Container Registry + Container Apps" -ForegroundColor Gray
    Write-Host "    - Managed Identity + RBAC role assignments" -ForegroundColor Gray
    Write-Host "  This may take 10-15 minutes on first deploy..." -ForegroundColor Gray
    Write-Host ""

    $deployResult = az deployment group create `
        --resource-group $ResourceGroupName `
        --template-file "infra/main.bicep" `
        --parameters location=$Location `
        --parameters baseName=$BaseName `
        --parameters foundryModel=$FoundryModel `
        --parameters bingConnectionName=$BingConnectionName `
        --output json 2>&1

    $deployExitCode = $LASTEXITCODE

    # Check for role assignment conflicts (non-fatal on re-deploy)
    $onlyRoleConflict = $false
    if ($deployExitCode -ne 0) {
        $deployStr = $deployResult -join "`n"
        if ($deployStr -match "RoleAssignmentExists" -and $deployStr -notmatch "(?!RoleAssignmentExists)(BadRequest|InvalidTemplate|ResourceNotFound|DeploymentFailed.*(?!RoleAssignmentExists)\w+Error)") {
            Write-Host "  WARNING: Role assignment already exists (safe to ignore on re-deploy)" -ForegroundColor Yellow
            $onlyRoleConflict = $true
        }
    }

    if ($deployExitCode -ne 0 -and -not $onlyRoleConflict) {
        Write-Host "ERROR: Infrastructure deployment failed!" -ForegroundColor Red
        Write-Host $deployResult -ForegroundColor Red
        Write-Host ""
        Write-Host "Common issues:" -ForegroundColor Yellow
        Write-Host "  - Model not available in region: Check 'az cognitiveservices account list-models --location $Location'" -ForegroundColor Gray
        Write-Host "  - Insufficient quota: Request more TPM quota via the Azure portal" -ForegroundColor Gray
        Write-Host "  - Bing provider not registered: Run 'az provider register --namespace Microsoft.Bing'" -ForegroundColor Gray
        exit 1
    }

    # Get outputs - from successful deployment or by querying resources directly
    if ($deployExitCode -eq 0) {
        $outputs = ($deployResult | Where-Object { $_ -notmatch "^WARNING:" }) -join "" | ConvertFrom-Json
        $outputs = $outputs.properties.outputs
    }

    # Always resolve resource info from Azure directly (works even after role assignment conflicts) 
    $acrName = az resource list --resource-group $ResourceGroupName --resource-type "Microsoft.ContainerRegistry/registries" --query "[0].name" -o tsv
    $acrLoginServer = az acr show --name $acrName --query loginServer -o tsv
    $aiServicesEndpoint = az cognitiveservices account show --name (az resource list --resource-group $ResourceGroupName --resource-type "Microsoft.CognitiveServices/accounts" --query "[0].name" -o tsv) --resource-group $ResourceGroupName --query "properties.endpoint" -o tsv
    $aiServicesName = az resource list --resource-group $ResourceGroupName --resource-type "Microsoft.CognitiveServices/accounts" --query "[0].name" -o tsv
    $aiProjectName = "${BaseName}-project"
    # Use .services.ai.azure.com endpoint (required by azure-ai-projects SDK)
    $aiProjectEndpoint = "https://${aiServicesName}.services.ai.azure.com/api/projects/${aiProjectName}"
    $bingGroundingName = az resource list --resource-group $ResourceGroupName --resource-type "Microsoft.Bing/accounts" --query "[0].name" -o tsv
    $aiHubName = az resource list --resource-group $ResourceGroupName --resource-type "Microsoft.MachineLearningServices/workspaces" --query "[?kind=='Hub'].name | [0]" -o tsv
    $appFqdn = az containerapp show --name "${BaseName}-app" --resource-group $ResourceGroupName --query "properties.configuration.ingress.fqdn" -o tsv 2>$null
    $appUrl = if ($appFqdn) { "https://$appFqdn" } else { "(pending)" }

    Write-Host "  Infrastructure deployed successfully!" -ForegroundColor Green
    Write-Host "    AI Services:      $aiServicesEndpoint" -ForegroundColor Gray
    Write-Host "    AI Project:       $aiProjectEndpoint" -ForegroundColor Gray
    Write-Host "    Bing Grounding:   $bingGroundingName" -ForegroundColor Gray
    Write-Host "    AI Hub:           $aiHubName" -ForegroundColor Gray
    Write-Host "    Container Registry: $acrLoginServer" -ForegroundColor Gray
    Write-Host "    App URL:          $appUrl" -ForegroundColor Gray
    Write-Host ""

    # ─────────────────────────────────────────────────
    # Step 4: Create Bing Grounding Connection
    # ─────────────────────────────────────────────────

    Write-Host "Step 4: Creating Bing Grounding connection in AI Hub..." -ForegroundColor Yellow
    
    # Get Grounding with Bing Search key from the newly created resource
    $bingKeyJson = az resource invoke-action `
        --action listKeys `
        --ids (az resource show --resource-group $ResourceGroupName --resource-type "Microsoft.Bing/accounts" --name $bingGroundingName --query id -o tsv) `
        --api-version "2020-06-10" `
        --output json 2>$null
    
    if ($bingKeyJson) {
        $bingKey = ($bingKeyJson | ConvertFrom-Json).key1

        # Create connection via REST API
        $token = az account get-access-token --query accessToken -o tsv
        $hubResourceId = az resource show --resource-group $ResourceGroupName --resource-type "Microsoft.MachineLearningServices/workspaces" --name $aiHubName --query id -o tsv 2>$null
        
        if ($hubResourceId) {
            $connectionBody = @{
                properties = @{
                    authType = "ApiKey"
                    category = "ApiKey"
                    isSharedToAll = $true
                    target = "https://api.bing.microsoft.com/"
                    credentials = @{
                        key = $bingKey
                    }
                    metadata = @{
                        ApiType = "Bing"
                    }
                }
            } | ConvertTo-Json -Depth 5

            $connectionUri = "https://management.azure.com${hubResourceId}/connections/${BingConnectionName}?api-version=2024-10-01"
            
            try {
                $null = Invoke-RestMethod -Method PUT -Uri $connectionUri `
                    -Headers @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" } `
                    -Body $connectionBody
                Write-Host "  Bing Grounding connection '$BingConnectionName' created successfully!" -ForegroundColor Green
            }
            catch {
                Write-Host "  WARNING: Could not create Bing connection automatically." -ForegroundColor Yellow
                Write-Host "  Create it manually in Azure AI Foundry portal:" -ForegroundColor Yellow
                Write-Host "    1. Go to https://ai.azure.com > your project > Connected resources" -ForegroundColor Gray
                Write-Host "    2. Add a new connection, type: API key" -ForegroundColor Gray
                Write-Host "    3. Name: $BingConnectionName" -ForegroundColor Gray
                Write-Host "    4. Endpoint: https://api.bing.microsoft.com/" -ForegroundColor Gray
                Write-Host "    5. Get the key from: az resource invoke-action --action listKeys --ids <bing-resource-id> --api-version 2020-06-10" -ForegroundColor Gray
            }
        }
    } else {
        Write-Host "  WARNING: Could not retrieve Bing Search key." -ForegroundColor Yellow
        Write-Host "  Create the Bing Grounding connection manually in AI Foundry portal." -ForegroundColor Gray
    }
    Write-Host ""

} else {
    # Skip infrastructure - just get existing resource names
    Write-Host "Step 3: Skipping infrastructure (--SkipInfrastructure)" -ForegroundColor Yellow
    
    $acrName = az resource list --resource-group $ResourceGroupName --resource-type "Microsoft.ContainerRegistry/registries" --query "[0].name" -o tsv
    $acrLoginServer = az acr show --name $acrName --query loginServer -o tsv
    $appUrl = az containerapp show --name "${BaseName}-app" --resource-group $ResourceGroupName --query "properties.configuration.ingress.fqdn" -o tsv
    if ($appUrl) { $appUrl = "https://$appUrl" }
    
    Write-Host "  Using existing resources:" -ForegroundColor Gray
    Write-Host "    ACR: $acrLoginServer" -ForegroundColor Gray
    Write-Host "    App: $appUrl" -ForegroundColor Gray
    Write-Host ""
}

# ─────────────────────────────────────────────────
# Step 5: Login to Container Registry
# ─────────────────────────────────────────────────

$stepNum = if ($SkipInfrastructure) { "Step 4" } else { "Step 5" }
Write-Host "${stepNum}: Logging into Container Registry..." -ForegroundColor Yellow
az acr login --name $acrName
Write-Host "  Logged into ACR." -ForegroundColor Green
Write-Host ""

# ─────────────────────────────────────────────────
# Step 6: Build and Push Docker Image
# ─────────────────────────────────────────────────

$stepNum = if ($SkipInfrastructure) { "Step 5" } else { "Step 6" }
Write-Host "${stepNum}: Building and pushing Docker image (ACR Tasks)..." -ForegroundColor Yellow
Write-Host "  This builds in the cloud (no local Docker needed)..." -ForegroundColor Gray

$imageName = "$acrLoginServer/${BaseName}:latest"

az acr build `
    --registry $acrName `
    --image "${BaseName}:latest" `
    --file Dockerfile `
    . 

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Docker build failed!" -ForegroundColor Red
    exit 1
}

Write-Host "  Docker image built and pushed successfully!" -ForegroundColor Green
Write-Host ""

# ─────────────────────────────────────────────────
# Step 7: Update Container App
# ─────────────────────────────────────────────────

$stepNum = if ($SkipInfrastructure) { "Step 6" } else { "Step 7" }
Write-Host "${stepNum}: Updating Container App with new image..." -ForegroundColor Yellow

# Configure ACR registry on the Container App and update the image
az containerapp registry set `
    --name "${BaseName}-app" `
    --resource-group $ResourceGroupName `
    --server $acrLoginServer `
    --identity system `
    --output none

az containerapp update `
    --name "${BaseName}-app" `
    --resource-group $ResourceGroupName `
    --image $imageName `
    --output none

Write-Host "  Container App updated!" -ForegroundColor Green
Write-Host ""

# ─────────────────────────────────────────────────
# Done!
# ─────────────────────────────────────────────────

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "DEPLOYMENT COMPLETE!" -ForegroundColor Green
Write-Host "====================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Your Vehicle Dimensions API is now available at:" -ForegroundColor White
Write-Host "  $appUrl" -ForegroundColor Yellow
Write-Host ""
Write-Host "API Endpoints:" -ForegroundColor White
Write-Host "  Health Check:     $appUrl/health" -ForegroundColor Gray
Write-Host "  Swagger Docs:     $appUrl/docs" -ForegroundColor Gray
Write-Host "  Vehicle Lookup:   POST $appUrl/api/v1/vehicle/lookup" -ForegroundColor Gray
Write-Host "  Batch Lookup:     POST $appUrl/api/v1/vehicle/batch" -ForegroundColor Gray
Write-Host "  Gov Makes:        GET $appUrl/api/v1/gov/makes" -ForegroundColor Gray
Write-Host "  Gov Models:       GET $appUrl/api/v1/gov/models/{make}" -ForegroundColor Gray
Write-Host ""
Write-Host "Resources created:" -ForegroundColor White
Write-Host "  Resource Group:     $ResourceGroupName" -ForegroundColor Gray
if (-not $SkipInfrastructure) {
    Write-Host "  AI Services:        $aiServicesName" -ForegroundColor Gray
    Write-Host "  AI Hub:             $aiHubName" -ForegroundColor Gray
    Write-Host "  AI Project:         $aiProjectName" -ForegroundColor Gray
    Write-Host "  Project Endpoint:   $aiProjectEndpoint" -ForegroundColor Gray
    Write-Host "  Bing Grounding:     $bingGroundingName" -ForegroundColor Gray
    Write-Host "  Bing Connection:    $BingConnectionName" -ForegroundColor Gray
    Write-Host "  Foundry Model:      $FoundryModel" -ForegroundColor Gray
}
Write-Host "  Container Registry: $acrLoginServer" -ForegroundColor Gray
Write-Host ""
Write-Host "Test with:" -ForegroundColor White
Write-Host "  curl $appUrl/health" -ForegroundColor Gray
Write-Host ""
