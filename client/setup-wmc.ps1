# =====================================================================
# MediaCenterServicePack - 1-Click Client Setup for Windows Media Center
# Pure native Windows 7 SP1 (PowerShell 2.0 / .NET 3.5) compatible.
# =====================================================================

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$ServerUrl = "http://mediacenter.local:8080"
)

$ErrorActionPreference = "Stop"

# 1. Administrator Check & Self-Elevation
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[WARN] Administrator rights required. Requesting elevation..." -ForegroundColor Yellow
    try {
        $scriptPath = $PSCommandPath
        if ($scriptPath -and (Test-Path $scriptPath)) {
            Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -ServerUrl `"$ServerUrl`"" -Verb RunAs
            exit 0
        } else {
            $bootstrap = "(New-Object System.Net.WebClient).DownloadString('$ServerUrl/setup.ps1') | Invoke-Expression"
            Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"$bootstrap`"" -Verb RunAs
            exit 0
        }
    } catch {
        Write-Host "[WARN] Self-elevation bypassed. Continuing in current shell..." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "   MediaCenterServicePack - Automated Windows Media Center Setup" -ForegroundColor White
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""

# 2. Server Discovery
$wc = New-Object System.Net.WebClient
$wc.Headers.Add("User-Agent", "MediaCenterServicePack-Setup/1.0")

function Find-ServerEndpoint {
    param([string]$Preferred)
    $list = @($Preferred, "http://mediacenter.local:8080", "http://mediacenter.local", "http://127.0.0.1:8080")
    foreach ($cand in $list) {
        if (-not $cand) { continue }
        try {
            $res = $wc.DownloadString("$($cand.TrimEnd('/'))/health")
            if ($res -match '"healthy"' -or $res -match '"version"') { return $cand.TrimEnd('/') }
        } catch {}
    }
    return "http://mediacenter.local:8080"
}

$activeServer = Find-ServerEndpoint -Preferred $ServerUrl
Write-Host "[1/3] Connected to server: $activeServer" -ForegroundColor Green

# 3. Install Synchronization Engine
$installDir = "$env:ProgramData\MediaCenterServicePack"
if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
}

$syncScript = Join-Path $installDir "sync-wmc.ps1"
$localSiblingSync = if ($PSScriptRoot) { Join-Path $PSScriptRoot "sync-wmc.ps1" } else { $null }

if ($localSiblingSync -and (Test-Path $localSiblingSync)) {
    Write-Host "[2/3] Installing local synchronization engine..." -ForegroundColor Cyan
    Copy-Item -Path $localSiblingSync -Destination $syncScript -Force
} else {
    Write-Host "[2/3] Downloading synchronization engine..." -ForegroundColor Cyan
    try {
        $wc.DownloadFile("$activeServer/api/client/sync-wmc.ps1", $syncScript)
    } catch {
        Write-Host "[ERROR] Could not download sync-wmc.ps1 from $activeServer : $_" -ForegroundColor Red
        exit 1
    }
}
$wc.Dispose()

# 4. Run Setup
Write-Host "[3/3] Ingesting TV Guide, artwork, and registering background tasks..." -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $syncScript -Setup -ServerUrl $activeServer

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host "  SETUP COMPLETE!" -ForegroundColor White
Write-Host "  Windows Media Center is now connected to MediaCenterServicePack." -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Green
exit 0
