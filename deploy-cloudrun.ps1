# Deploy Meta MCP connector to Google Cloud Run (mirrors vdp-connector / DV360 pattern).
#
# Usage:
#   .\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID"
#   .\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID" -Region "us-central1" -Service "meta-mcp"
#   .\deploy-cloudrun.ps1 -ProjectId "..." -SecretFile ".\facebook-app-secret.key"
#
# Secrets (Secret Manager — never committed):
#   FACEBOOK_APP_SECRET      → meta-facebook-app-secret
#   FACEBOOK_ACCESS_TOKEN    → meta-facebook-access-token (optional but recommended)
#
# Non-secret env vars on Cloud Run:
#   FACEBOOK_APP_ID, FACEBOOK_AD_ACCOUNT_ID, MCP_PUBLIC_URL (set after deploy)

param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [string]$Region = "us-central1",
    [string]$Service = "meta-mcp",
    [string]$AppSecretName = "meta-facebook-app-secret",
    [string]$AccessTokenSecretName = "meta-facebook-access-token",
    [string]$SecretFile = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path -LiteralPath ".\Dockerfile")) {
    throw "Dockerfile not found in $PSScriptRoot"
}
if (-not (Test-Path -LiteralPath ".\meta_mcp_server.py")) {
    throw "meta_mcp_server.py not found in $PSScriptRoot"
}

function Get-EnvValue([string]$Key) {
    $envPath = Join-Path $PSScriptRoot ".env"
    if (-not (Test-Path -LiteralPath $envPath)) { return "" }
    foreach ($line in Get-Content -LiteralPath $envPath) {
        $t = $line.Trim()
        if ($t -match '^\s*#' -or $t -eq "") { continue }
        if ($t -match "^(?:export\s+)?$Key\s*=\s*(.*)$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function Read-SecretValue([string]$Key, [string]$Prompt) {
    if ($SecretFile -and (Test-Path -LiteralPath $SecretFile)) {
        return (Get-Content -LiteralPath $SecretFile -Raw).Trim()
    }
    $fromEnv = Get-EnvValue $Key
    if ($fromEnv) { return $fromEnv }

    Write-Host "$Prompt (input hidden), then Enter:"
    $secure = Read-Host -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Ensure-Secret([string]$Name, [string]$Value) {
    if (-not $Value) { throw "Secret value for $Name is empty." }
    $tmp = Join-Path $env:TEMP "meta-secret-$([guid]::NewGuid().ToString('N')).txt"
    try {
        Set-Content -LiteralPath $tmp -Value $Value -NoNewline -Encoding utf8
        $exists = $null
        try { $exists = gcloud secrets describe $Name --project $ProjectId 2>$null } catch { $exists = $null }
        if (-not $exists) {
            Write-Host "Creating secret $Name ..."
            gcloud secrets create $Name --project $ProjectId --replication-policy=automatic --data-file=$tmp
        } else {
            Write-Host "Adding new version to secret $Name ..."
            gcloud secrets versions add $Name --project $ProjectId --data-file=$tmp
        }
    } finally {
        if (Test-Path -LiteralPath $tmp) {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "Using project $ProjectId / region $Region / service $Service"

gcloud config set project $ProjectId

gcloud services enable `
    run.googleapis.com `
    cloudbuild.googleapis.com `
    secretmanager.googleapis.com `
    artifactregistry.googleapis.com `
    --project $ProjectId

$appId = Get-EnvValue "FACEBOOK_APP_ID"
$adAccount = Get-EnvValue "FACEBOOK_AD_ACCOUNT_ID"
if (-not $appId) { throw "FACEBOOK_APP_ID missing in .env" }
if (-not $adAccount) { throw "FACEBOOK_AD_ACCOUNT_ID missing in .env" }

$appSecret = Read-SecretValue "FACEBOOK_APP_SECRET" "Paste FACEBOOK_APP_SECRET"
if (-not $appSecret) { throw "FACEBOOK_APP_SECRET is empty. Pass -SecretFile or set it in .env" }
Ensure-Secret $AppSecretName $appSecret

$accessToken = Get-EnvValue "FACEBOOK_ACCESS_TOKEN"
$useAccessTokenSecret = $false
if ($accessToken) {
    Ensure-Secret $AccessTokenSecretName $accessToken
    $useAccessTokenSecret = $true
} else {
    Write-Host "WARNING: FACEBOOK_ACCESS_TOKEN not in .env — deploy will lack ads_read. Add it to .env and redeploy."
}

$projectNumber = (gcloud projects describe $ProjectId --format="value(projectNumber)").Trim()

$secretsArg = "FACEBOOK_APP_SECRET=${AppSecretName}:latest"
if ($useAccessTokenSecret) {
    $secretsArg += ",FACEBOOK_ACCESS_TOKEN=${AccessTokenSecretName}:latest"
}

Write-Host "Building and deploying from source (Dockerfile)..."

# Durable Claude OAuth sessions (survives Cloud Run deploys — /tmp alone causes session timeouts)
$oauthBucket = "meta-mcp-oauth-state-$ProjectId"
$bucketExists = $null
try { $bucketExists = gcloud storage buckets describe "gs://$oauthBucket" --project $ProjectId 2>$null } catch { $bucketExists = $null }
if (-not $bucketExists) {
    Write-Host "Creating OAuth state bucket gs://$oauthBucket ..."
    gcloud storage buckets create "gs://$oauthBucket" --project $ProjectId --location=$Region --uniform-bucket-level-access
}

# Allow the Cloud Run runtime SA to read/write OAuth state
$runtimeSa = "$projectNumber-compute@developer.gserviceaccount.com"
gcloud storage buckets add-iam-policy-binding "gs://$oauthBucket" `
    --member="serviceAccount:$runtimeSa" `
    --role="roles/storage.objectAdmin" `
    --project $ProjectId 2>$null

gcloud run deploy $Service `
    --project $ProjectId `
    --region $Region `
    --source . `
    --command python `
    --args "meta_mcp_server.py" `
    --set-env-vars "MCP_TRANSPORT=http,HOST=0.0.0.0,FACEBOOK_APP_ID=$appId,FACEBOOK_AD_ACCOUNT_ID=$adAccount,CLOUD_RUN_REGION=$Region,CLOUD_RUN_PROJECT_NUMBER=$projectNumber,MCP_OAUTH_GCS_BUCKET=$oauthBucket" `
    --set-secrets $secretsArg `
    --allow-unauthenticated `
    --session-affinity `
    --min-instances 1 `
    --max-instances 1 `
    --timeout 300 `
    --port 8080

$url = gcloud run services describe $Service `
    --project $ProjectId `
    --region $Region `
    --format="value(status.url)"

Write-Host "Setting MCP_PUBLIC_URL=$url for Claude OAuth discovery..."
gcloud run services update $Service `
    --project $ProjectId `
    --region $Region `
    --update-env-vars "MCP_PUBLIC_URL=$url,MCP_OAUTH_GCS_BUCKET=$oauthBucket"

Write-Host ""
Write-Host "Deployed. MCP connector URL (use this exact path):"
Write-Host "  $url/mcp"
Write-Host ""
Write-Host "Verify OAuth discovery:"
Write-Host "  $url/.well-known/oauth-authorization-server"
Write-Host ""
Write-Host "Cursor mcp.json:"
Write-Host ("  `"meta`": { `"url`": `"$url/mcp`" }")
Write-Host ""
Write-Host "Claude.ai:"
Write-Host "  Settings -> Connectors -> Add custom connector"
Write-Host "  Name: Meta Ads (or any name)"
Write-Host "  URL:  $url/mcp"
Write-Host "  Leave Advanced OAuth Client ID empty (server supports DCR)."
Write-Host "  Click Connect — browser may briefly authorize, then tools appear."
Write-Host ""
Write-Host "Security note: service is publicly reachable; OAuth restricts tokens to Claude redirects."
