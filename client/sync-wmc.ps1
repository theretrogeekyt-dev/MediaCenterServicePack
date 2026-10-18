<#
.SYNOPSIS
    Project eHome (OpenMCE) - Client Ingestion Helper for Windows Media Center.

.DESCRIPTION
    Downloads compiled guide.mxf from the OpenMCE Docker container, ingests it
    into Windows Media Center using Microsoft's native loadmxf.exe, and refreshes
    the eHome cache. Also supports directly downloading movie metadata and album
    artwork packages from established sources without local media folder requirements.

.PARAMETER ServerUrl
    The HTTP URL to the OpenMCE backend container, e.g. "http://192.168.1.100:8080".

.PARAMETER WorkingDirectory
    Local directory to store downloaded MXF guide files and logs. Defaults to "%ProgramData%\OpenMCE".

.PARAMETER DownloadMovie
    Optional: Title or TMDb ID of a movie to directly download WMC metadata sidecars for.

.PARAMETER DownloadAlbum
    Optional: Title or MBID of a music album to directly download 500x500 WMC artwork for.

.PARAMETER SkipServiceRestart
    If set, skips stopping/starting ehsched and ehrecvr services during ingestion.

.PARAMETER Quiet
    Suppresses console output (useful for silent Task Scheduler jobs).

.EXAMPLE
    .\sync-wmc.ps1 -ServerUrl "http://192.168.1.100:8080"
    .\sync-wmc.ps1 -ServerUrl "http://192.168.1.100:8080" -DownloadMovie "Inception"
#>

[CmdletBinding()]
param (
    [Parameter(Mandatory = $false)]
    [string]$ServerUrl = "http://localhost:8080",

    [Parameter(Mandatory = $false)]
    [string]$WorkingDirectory = "$env:ProgramData\OpenMCE",

    [Parameter(Mandatory = $false)]
    [string]$DownloadMovie = $null,

    [Parameter(Mandatory = $false)]
    [string]$DownloadAlbum = $null,

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

    if ($WorkingDirectory) {
        try {
            $logPath = Join-Path $WorkingDirectory "sync-wmc.log"
            Add-Content -Path $logPath -Value $formatted -ErrorAction SilentlyContinue
        } catch {}
    }
}

Log-Message "=== Project eHome (OpenMCE) Windows Client Tool ==="

# 1. Check Administrator Privileges
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Log-Message "Notice: Running without elevated administrator privileges. Guide ingestion (loadmxf) may require admin rights." "WARN"
}

# 2. Prepare Working Directory
if (-not (Test-Path $WorkingDirectory)) {
    New-Item -ItemType Directory -Path $WorkingDirectory -Force | Out-Null
}

$webClient = New-Object System.Net.WebClient
$webClient.Headers.Add("User-Agent", "OpenMCE-WMC-Client/1.0")

# 3. Direct Movie Metadata Download (Optional)
if ($DownloadMovie) {
    Log-Message "Querying backend for movie metadata: '$DownloadMovie'..."
    $movieZipPath = Join-Path $WorkingDirectory "movie_metadata.zip"
    $movieEndpoint = "$($ServerUrl.TrimEnd('/'))/api/metadata/movies/$([System.Uri]::EscapeDataString($DownloadMovie))/download"
    try {
        $webClient.DownloadFile($movieEndpoint, $movieZipPath)
        Log-Message "Downloaded movie metadata bundle to: $movieZipPath" "SUCCESS"

        # Expand archive
        $movieOutDir = Join-Path $WorkingDirectory "Movies\$($DownloadMovie -replace '[^\w\s]', '')"
        if (-not (Test-Path $movieOutDir)) { New-Item -ItemType Directory -Path $movieOutDir -Force | Out-Null }
        [System.IO.Compression.ZipFile]::ExtractToDirectory($movieZipPath, $movieOutDir)
        Log-Message "Extracted dvdid.xml and folder.jpg to $movieOutDir" "SUCCESS"

        # Also copy to WMC DvdInfoCache if present
        $wmcDvdCache = "$env:LOCALAPPDATA\Microsoft\eHome\DvdInfoCache"
        if (Test-Path $wmcDvdCache) {
            Get-ChildItem -Path $movieOutDir -Filter "*.xml" | ForEach-Object {
                Copy-Item -Path $_.FullName -Destination $wmcDvdCache -Force
                Log-Message "Copied metadata to WMC DvdInfoCache: $($_.Name)" "SUCCESS"
            }
        }
    } catch {
        Log-Message "Failed downloading movie metadata: $_" "ERROR"
    }
}

# 4. Direct Music Artwork Download (Optional)
if ($DownloadAlbum) {
    Log-Message "Querying backend for album artwork: '$DownloadAlbum'..."
    $albumZipPath = Join-Path $WorkingDirectory "music_art.zip"
    $musicEndpoint = "$($ServerUrl.TrimEnd('/'))/api/metadata/music/$([System.Uri]::EscapeDataString($DownloadAlbum))/download"
    try {
        $webClient.DownloadFile($musicEndpoint, $albumZipPath)
        Log-Message "Downloaded music artwork bundle to: $albumZipPath" "SUCCESS"

        # Expand archive
        $albumOutDir = Join-Path $WorkingDirectory "Music\$($DownloadAlbum -replace '[^\w\s]', '')"
        if (-not (Test-Path $albumOutDir)) { New-Item -ItemType Directory -Path $albumOutDir -Force | Out-Null }
        [System.IO.Compression.ZipFile]::ExtractToDirectory($albumZipPath, $albumOutDir)
        Log-Message "Extracted 500x500 Folder.jpg and AlbumArtSmall.jpg to $albumOutDir" "SUCCESS"

        # Also copy to WMC art_cache if present
        $wmcArtCache = "$env:LOCALAPPDATA\Microsoft\eHome\art_cache"
        if (Test-Path $wmcArtCache) {
            Copy-Item -Path (Join-Path $albumOutDir "Folder.jpg") -Destination $wmcArtCache -Force -ErrorAction SilentlyContinue
            Log-Message "Copied 500x500 artwork into WMC art_cache." "SUCCESS"
        }
    } catch {
        Log-Message "Failed downloading music artwork: $_" "ERROR"
    }
}

# 5. Guide Ingestion Routine (Skip if running specific media downloads only)
if (-not $DownloadMovie -and -not $DownloadAlbum) {
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
        Log-Message "Could not locate loadmxf.exe. Is Windows Media Center installed?" "WARN"
    } else {
        $mxfLocalPath = Join-Path $WorkingDirectory "guide.mxf"
        $mxfEndpoint = "$($ServerUrl.TrimEnd('/'))/api/guide.mxf"
        Log-Message "Downloading guide payload from: $mxfEndpoint"

        try {
            $webClient.DownloadFile($mxfEndpoint, $mxfLocalPath)
        } catch {
            Log-Message "Failed to download guide.mxf from $mxfEndpoint : $_" "ERROR"
            exit 3
        }

        if (-not $SkipServiceRestart) {
            Log-Message "Halting WMC background scheduler & receiver services..."
            Stop-Service -Name "ehsched" -Force -ErrorAction SilentlyContinue
            Stop-Service -Name "ehrecvr" -Force -ErrorAction SilentlyContinue
        }

        Log-Message "Executing loadmxf.exe on guide payload..."
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

        if (-not $SkipServiceRestart) {
            Log-Message "Restarting WMC services..."
            Start-Service -Name "ehsched" -ErrorAction SilentlyContinue
            Start-Service -Name "ehrecvr" -ErrorAction SilentlyContinue
        }

        $ehPrivJob = Join-Path $ehomeDir "ehPrivJob.exe"
        if (Test-Path $ehPrivJob) {
            Log-Message "Refreshing eHome caches via ehPrivJob..."
            Start-Process -FilePath $ehPrivJob -ArgumentList "/DoReIndexing" -Wait -NoNewWindow -ErrorAction SilentlyContinue
            Start-Process -FilePath $ehPrivJob -ArgumentList "/UpdateDiscMetadata" -Wait -NoNewWindow -ErrorAction SilentlyContinue
        }
    }
}

Log-Message "=== Task Complete! ===" "SUCCESS"
exit 0
