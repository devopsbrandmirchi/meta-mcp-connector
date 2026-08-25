# Deploy Meta MCP connector to Google Cloud Run.
#
# Usage:
#   .\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID"
#   .\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID" -Region "us-central1" -Service "meta-mcp"
#
# Secrets: FACEBOOK_APP_SECRET via Secret Manager.
# Non-secret env: FACEBOOK_APP_ID, FACEBOOK_AD_ACCOUNT_ID

param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [string]$Region = "us-central1",
    [string]$Service = "meta-mcp",
    [string]$AppSecretName = "meta-facebook-app-secret"
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

function Ensure-Secret([string]$Name, [string]$Value) {
    if (-not $Value) { throw "Secret value for $Name is empty. Set it in .env" }
    $tmp = Join-Path $env:TEMP "meta-secret-$([guid]::NewGuid().ToString('N')).txt"
    try {
        Set-Content -LiteralPath $tmp -Value $Value -NoNewline -Encoding utf8
        $exists = $null
        try { $exists = gcloud secrets describe $Name --project $ProjectId 2>$null } catch { $exists = $null }
        if (-not $exists) {
            gcloud secrets create $Name --project $ProjectId --replication-policy=automatic --data-file=$tmp
        } else {
            gcloud secrets versions add $Name --project $ProjectId --data-file=$tmp
        }
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
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
$appSecret = Get-EnvValue "FACEBOOK_APP_SECRET"

if (-not $appId) { throw "FACEBOOK_APP_ID missing in .env" }
if (-not $adAccount) { throw "FACEBOOK_AD_ACCOUNT_ID missing in .env" }

Ensure-Secret $AppSecretName $appSecret

Write-Host "Building and deploying from source (Dockerfile)..."
gcloud run deploy $Service `
    --project $ProjectId `
    --region $Region `
    --source . `
    --command python `
    --args "meta_mcp_server.py" `
    --set-env-vars "MCP_TRANSPORT=http,HOST=0.0.0.0,FACEBOOK_APP_ID=$appId,FACEBOOK_AD_ACCOUNT_ID=$adAccount" `
    --set-secrets "FACEBOOK_APP_SECRET=${AppSecretName}:latest" `
    --allow-unauthenticated `
    --session-affinity `
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
    --update-env-vars "MCP_PUBLIC_URL=$url"

Write-Host ""
Write-Host "Deployed. MCP connector URL:"
Write-Host "  $url/mcp"
Write-Host ""
Write-Host 'Cursor mcp.json:'
Write-Host ("  `"meta`": { `"url`": `"$url/mcp`" }")
