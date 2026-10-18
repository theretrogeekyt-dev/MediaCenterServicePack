<#
.SYNOPSIS
    Project eHome (OpenMCE) - Client Ingestion Helper for Windows Media Center.

.DESCRIPTION
    Downloads compiled guide.mxf from the OpenMCE Docker container, ingests it
    into Windows Media Center using Microsoft's native loadmxf.exe, and refreshes
    the eHome cache. Suitable for running via Windows Task Scheduler.

.PARAMETER ServerUrl
    The HTTP URL to the OpenMCE backend container, e.g. "http://192.168.1.100:8080".

.PARAMETER WorkingDirectory
    Local directory to store downloaded MXF guide files and logs. Defaults to "%ProgramData%\OpenMCE".

.PARAMETER SkipServiceRestart
    If set, skips stopping/starting ehsched and ehrecvr services during ingestion.

.PARAMETER Quiet
    Suppresses console output (useful for silent Task Scheduler jobs).

.EXAMPLE
    .\sync-wmc.ps1 -ServerUrl "http://192.168.1.100:8080"
#>

[CmdletBinding()]
param (
    [Parameter(Mandatory = $false)]
    [string]$ServerUrl = "http://localhost:8080",

    [Parameter(Mandatory = $false)]
    [string]$WorkingDirectory = "$env:ProgramData\OpenMCE",

    [Parameter(Mandatory = $false)]
    [switch]$SkipServiceRestart = $false,

    [Parameter(Mandatory = $false)]
    [switch]$Quiet = $false
)

$ErrorActionPreference = "Stop"

function Log-Message {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $formatted = "[$timestamp] [$Level] $Message"
    
    if (-not $Quiet) {
        switch ($Level) {
            "ERROR" { Write-Host $formatted -ForegroundColor Red }
            "WARN"  { Write-Host $formatted -ForegroundColor Yellow }
            "SUCCESS" { Write-Host $formatted -ForegroundColor Green }
            default { Write-Host $formatted -ForegroundColor Cyan }
        }
    }

    # Write to local log file
    if ($WorkingDirectory) {
        try {
            $logPath = Join-Path $WorkingDirectory "sync-wmc.log"
            Add-Content -Path $logPath -Value $formatted -ErrorAction SilentlyContinue
        } catch {}
    }
}

Log-Message "=== Starting Windows Media Center EPG Sync ==="

# 1. Check Administrator Privileges
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Log-Message "This script must be executed with elevated Administrator privileges to manage WMC services and loadmxf." "ERROR"
    exit 1
}

# 2. Locate loadmxf.exe
$ehomeCandidates = @(
    "$env:SystemRoot\ehome",
    "$env:SystemRoot\SysWOW64\ehome",
    "$env:SystemDrive\Windows\ehome"
)

$loadMxfPath = $null
$ehomeDir = $null
foreach ($dir in $ehomeCandidates) {
    $testPath = Join-Path $dir "loadmxf.exe"
    if (Test-Path $testPath) {
        $loadMxfPath = $testPath
        $ehomeDir = $dir
        break
    }
}

if (-not $loadMxfPath) {
    Log-Message "Could not locate loadmxf.exe. Is Windows Media Center installed on this machine?" "ERROR"
    exit 2
}
Log-Message "Located WMC Ingestion Utility: $loadMxfPath"

# 3. Prepare Working Directory
if (-not (Test-Path $WorkingDirectory)) {
    New-Item -ItemType Directory -Path $WorkingDirectory -Force | Out-Null
}
$mxfLocalPath = Join-Path $WorkingDirectory "guide.mxf"

# 4. Download guide.mxf from OpenMCE Container
$mxfEndpoint = "$($ServerUrl.TrimEnd('/'))/api/guide.mxf"
Log-Message "Downloading guide payload from: $mxfEndpoint"

try {
    # Use WebClient for maximum compatibility across older Windows / PowerShell 2.0+
    $webClient = New-Object System.Net.WebClient
    $webClient.Headers.Add("User-Agent", "OpenMCE-WMC-Client/1.0")
    $webClient.DownloadFile($mxfEndpoint, $mxfLocalPath)
} catch {
    Log-Message "Failed to download guide.mxf from $mxfEndpoint : $_" "ERROR"
    exit 3
}

# Validate downloaded file
if (-not (Test-Path $mxfLocalPath) -or ((Get-Item $mxfLocalPath).Length -lt 100)) {
    Log-Message "Downloaded guide.mxf is missing or invalid (size < 100 bytes)." "ERROR"
    exit 4
}
$fileSizeKb = [math]::Round((Get-Item $mxfLocalPath).Length / 1024, 2)
Log-Message "Successfully downloaded guide.mxf ($fileSizeKb KB)." "SUCCESS"

# 5. Stop WMC Background Services (if not skipped)
if (-not $SkipServiceRestart) {
    Log-Message "Temporarily halting WMC background scheduler & receiver services..."
    Stop-Service -Name "ehsched" -Force -ErrorAction SilentlyContinue
    Stop-Service -Name "ehrecvr" -Force -ErrorAction SilentlyContinue
}

# 6. Run loadmxf.exe to ingest the MXF database
Log-Message "Executing: & `"$loadMxfPath`" -i `"$mxfLocalPath`""
try {
    $proc = Start-Process -FilePath $loadMxfPath -ArgumentList "-i `"$mxfLocalPath`"" -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -eq 0) {
        Log-Message "loadmxf.exe completed successfully (ExitCode: 0)." "SUCCESS"
    } else {
        Log-Message "loadmxf.exe exited with non-zero exit code: $($proc.ExitCode)" "WARN"
    }
} catch {
    Log-Message "Error executing loadmxf.exe: $_" "ERROR"
}

# 7. Restart WMC Background Services
if (-not $SkipServiceRestart) {
    Log-Message "Restarting WMC services..."
    Start-Service -Name "ehsched" -ErrorAction SilentlyContinue
    Start-Service -Name "ehrecvr" -ErrorAction SilentlyContinue
}

# 8. Refresh eHome Thumbnail / Re-index Cache
$ehPrivJob = Join-Path $ehomeDir "ehPrivJob.exe"
if (Test-Path $ehPrivJob) {
    Log-Message "Triggering eHome index and metadata refresh via ehPrivJob..."
    try {
        Start-Process -FilePath $ehPrivJob -ArgumentList "/DoReIndexing" -Wait -NoNewWindow -ErrorAction SilentlyContinue
        Start-Process -FilePath $ehPrivJob -ArgumentList "/UpdateDiscMetadata" -Wait -NoNewWindow -ErrorAction SilentlyContinue
    } catch {}
}

Log-Message "=== EPG Sync Complete! TV Guide is now updated in Windows Media Center. ===" "SUCCESS"
exit 0

