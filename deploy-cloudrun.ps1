# Deploy VDP MCP connector to Google Cloud Run (mirrors DV360MCP/deploy-cloudrun.ps1).
#
# Usage:
#   .\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID"
#   .\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID" -Region "us-central1" -Service "vdp-mcp"
#   .\deploy-cloudrun.ps1 -ProjectId "..." -SecretFile ".\service-role.key"
#
# Secret: uploads SUPABASE_SERVICE_ROLE_KEY to Secret Manager (never commits it).
# Reads from -SecretFile, or .env (SUPABASE_SERVICE_ROLE_KEY=...), or prompts.

param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [string]$Region = "us-central1",
    [string]$Service = "vdp-mcp",
    [string]$SecretName = "vdp-supabase-service-role",
    [string]$SecretFile = "",
    [string]$SupabaseUrl = "https://rllwmeqingvuohyctddg.supabase.co"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path -LiteralPath ".\Dockerfile")) {
    throw "Dockerfile not found in $PSScriptRoot"
}
if (-not (Test-Path -LiteralPath ".\vdp_mcp_server.py")) {
    throw "vdp_mcp_server.py not found in $PSScriptRoot"
}

function Get-ServiceRoleKey {
    if ($SecretFile -and (Test-Path -LiteralPath $SecretFile)) {
        return (Get-Content -LiteralPath $SecretFile -Raw).Trim()
    }

    $envPath = Join-Path $PSScriptRoot ".env"
    if (Test-Path -LiteralPath $envPath) {
        foreach ($line in Get-Content -LiteralPath $envPath) {
            $t = $line.Trim()
            if ($t -match '^\s*#' -or $t -eq "") { continue }
            if ($t -match '^(?:export\s+)?SUPABASE_SERVICE_ROLE_KEY\s*=\s*(.*)$') {
                $val = $Matches[1].Trim().Trim('"').Trim("'")
                if ($val) { return $val }
            }
        }
    }

    Write-Host "Paste SUPABASE_SERVICE_ROLE_KEY (input hidden), then Enter:"
    $secure = Read-Host -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
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

$key = Get-ServiceRoleKey
if (-not $key) {
    throw "SUPABASE_SERVICE_ROLE_KEY is empty. Pass -SecretFile or set it in .env"
}

$tmpSecret = Join-Path $env:TEMP "vdp-supabase-service-role-$([guid]::NewGuid().ToString('N')).txt"
try {
    Set-Content -LiteralPath $tmpSecret -Value $key -NoNewline -Encoding utf8

    $secretExists = $null
    try {
        $secretExists = gcloud secrets describe $SecretName --project $ProjectId 2>$null
    } catch {
        $secretExists = $null
    }

    if (-not $secretExists) {
        Write-Host "Creating secret $SecretName ..."
        gcloud secrets create $SecretName `
            --project $ProjectId `
            --replication-policy=automatic `
            --data-file=$tmpSecret
    } else {
        Write-Host "Adding new version to secret $SecretName ..."
        gcloud secrets versions add $SecretName `
            --project $ProjectId `
            --data-file=$tmpSecret
    }
}
finally {
    if (Test-Path -LiteralPath $tmpSecret) {
        Remove-Item -LiteralPath $tmpSecret -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Building and deploying from source (Dockerfile)..."
gcloud run deploy $Service `
    --project $ProjectId `
    --region $Region `
    --source . `
    --command python `
    --args "vdp_mcp_server.py" `
    --set-env-vars "MCP_TRANSPORT=http,HOST=0.0.0.0,SUPABASE_URL=$SupabaseUrl" `
    --set-secrets "SUPABASE_SERVICE_ROLE_KEY=${SecretName}:latest" `
    --allow-unauthenticated `
    --session-affinity `
    --max-instances 1 `
    --timeout 300 `
    --port 8080

$url = gcloud run services describe $Service `
    --project $ProjectId `
    --region $Region `
    --format="value(status.url)"

# Claude OAuth discovery needs the public HTTPS origin as MCP_PUBLIC_URL.
Write-Host "Setting MCP_PUBLIC_URL=$url for Claude OAuth discovery..."
gcloud run services update $Service `
    --project $ProjectId `
    --region $Region `
    --update-env-vars "MCP_PUBLIC_URL=$url"

Write-Host ""
Write-Host "Deployed. MCP connector URL (use this exact path):"
Write-Host "  $url/mcp"
Write-Host ""
Write-Host "Cursor mcp.json:"
Write-Host ("  `"vdp`": { `"url`": `"$url/mcp`" }")
Write-Host ""
Write-Host "Claude.ai:"
Write-Host "  Settings -> Connectors -> Add custom connector"
Write-Host "  Name: VDP Report (or any name)"
Write-Host "  URL:  $url/mcp"
Write-Host "  Leave Advanced OAuth Client ID empty (server supports DCR)."
Write-Host "  Click Connect — browser may briefly authorize, then tools appear."
Write-Host ""
Write-Host "Security note: service is publicly reachable; OAuth restricts tokens to Claude redirects."
