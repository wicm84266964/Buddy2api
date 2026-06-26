$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

function Convert-ToDockerPath {
    param([Parameter(Mandatory=$true)][string]$Path)
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    return $resolved -replace "\\", "/"
}

Write-Host ""
Write-Host "  ========================================"
Write-Host "   Buddy 2 API Docker for Windows"
Write-Host "  ========================================"
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker was not found. Please start Docker Desktop first."
}

$defaultAuthDir = Join-Path $env:LOCALAPPDATA "CodeBuddyExtension\Data\Public\auth"
$authDir = if ($env:CB_HOST_AUTH_DIR) { $env:CB_HOST_AUTH_DIR } else { $defaultAuthDir }

if (-not (Test-Path -LiteralPath $authDir -PathType Container)) {
    Write-Host "  [hint] Default auth directory was not found: $authDir" -ForegroundColor Yellow
    Write-Host "  Confirm Work Buddy is logged in, or set the path manually:" -ForegroundColor Yellow
    Write-Host '  $env:CB_HOST_AUTH_DIR="C:\Users\YOUR_NAME\AppData\Local\CodeBuddyExtension\Data\Public\auth"'
    Write-Host "  .\start-docker-win.ps1"
    exit 1
}

$dockerAuthDir = Convert-ToDockerPath $authDir
$env:CB_HOST_AUTH_DIR = $dockerAuthDir

if (-not $env:CB_GATEWAY_ADMIN_TOKEN) {
    $env:CB_GATEWAY_ADMIN_TOKEN = "change-this-token"
}

Write-Host "  [auth] $authDir"
Write-Host "  [mount] $dockerAuthDir -> /auth:ro"
Write-Host "  [start] http://127.0.0.1:8787"
Write-Host ""

docker compose -f docker-compose.yml -f docker-compose.windows.yml up -d --build

Write-Host ""
Write-Host "  Started. Open http://127.0.0.1:8787, then rescan/import accounts on the Accounts page."
