<#
.SYNOPSIS
    MediaCenterServicePack - Client Ingestion Helper for Windows Media Center.

.DESCRIPTION
    Downloads compiled guide.mxf from the MediaCenterServicePack Docker container, ingests it
    into Windows Media Center using Microsoft's native loadmxf.exe, and refreshes
    the eHome cache. Also supports directly downloading movie metadata and album
    artwork packages from established sources without local media folder requirements.

.PARAMETER ServerUrl
    The HTTP URL to the MediaCenterServicePack backend container, e.g. "http://192.168.1.100:8080".

.PARAMETER WorkingDirectory
    Local directory to store downloaded MXF guide files and logs. Defaults to "%ProgramData%\MediaCenterServicePack".

.PARAMETER DownloadMovie
    Optional: Title or TMDb ID of a movie to directly download WMC metadata sidecars for.

.PARAMETER DownloadAlbum
    Optional: Title or MBID of a music album to directly download 500x500 WMC artwork for.

.PARAMETER MusicFolder
    Optional: Path to music directory (e.g. "C:\Users\User\Music"). Scans for albums missing Folder.jpg, auto-downloads 500x500 artwork, and injects into WMC.

.PARAMETER RefreshMusic
    Optional switch: Clears stale WMC art_cache and triggers ehPrivJob.exe /DoReIndexing to refresh the music library.

.PARAMETER ResetMusicDatabase
    Optional switch: Stops WMP Network Sharing Service, deletes corrupt or stale CurrentDatabase_*.wmdb library database (audio files are never touched), and forces a clean rescan.

.PARAMETER SkipServiceRestart
    If set, skips stopping/starting ehsched and ehrecvr services during ingestion.

.PARAMETER Quiet
    Suppresses console output (useful for silent Task Scheduler jobs).

.EXAMPLE
    .\sync-wmc.ps1
    .\sync-wmc.ps1 -RefreshMusic
    .\sync-wmc.ps1 -MusicFolder "C:\Users\New User\Music"
    .\sync-wmc.ps1 -DownloadAlbum "Abbey Road"
#>

[CmdletBinding()]
param (
    [Parameter(Mandatory = $false)]
    [string]$ServerUrl = "http://localhost:8080",

    [Parameter(Mandatory = $false)]
    [string]$WorkingDirectory = "$env:ProgramData\MediaCenterServicePack",

    [Parameter(Mandatory = $false)]
    [string]$DownloadMovie = $null,

    [Parameter(Mandatory = $false)]
    [string]$DownloadAlbum = $null,

    [Parameter(Mandatory = $false)]
    [string]$MusicFolder = $null,

    [Parameter(Mandatory = $false)]
    [switch]$RefreshMusic = $false,

    [Parameter(Mandatory = $false)]
    [switch]$ResetMusicDatabase = $false,

    [Parameter(Mandatory = $false)]
    [string]$ApiKey = $null,

    [Parameter(Mandatory = $false)]
    [switch]$SyncNow = $false,

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

Log-Message "=== MediaCenterServicePack Windows Client Tool ==="

# 1. Check Administrator Privileges
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Log-Message "Notice: Running without elevated administrator privileges. Guide ingestion (loadmxf) may require admin rights." "WARN"
}

# 2. Prepare Working Directory
if (-not (Test-Path $WorkingDirectory)) {
    New-Item -ItemType Directory -Path $WorkingDirectory -Force | Out-Null
}

# 3. Automatic Server Discovery (via DNS / host probes)
function Discover-Server {
    param([string]$DefaultUrl)

    # If user explicitly supplied a non-default ServerUrl, test it first
    if ($PSBoundParameters.ContainsKey('ServerUrl')) {
        try {
            $resp = Invoke-RestMethod -Uri "$($DefaultUrl.TrimEnd('/'))/health" -TimeoutSec 3 -ErrorAction Stop
            if ($resp.status -eq "healthy" -or $resp.version) {
                return $DefaultUrl
            }
        } catch {
            Log-Message "Notice: Provided ServerUrl '$DefaultUrl' failed health check. Probing auto-discovery..." "WARN"
        }
    } else {
        # Check localhost first
        try {
            $resp = Invoke-RestMethod -Uri "$($DefaultUrl.TrimEnd('/'))/health" -TimeoutSec 1 -ErrorAction Stop
            if ($resp.status -eq "healthy" -or $resp.version) {
                return $DefaultUrl
            }
        } catch {}
    }

    Log-Message "Auto-discovering MediaCenterServicePack server via DNS and local network..."
    $candidates = @(
        "http://mediacenter.local:8080",
        "http://mediacenter.local",
        "http://wmc.local:8080",
        "http://wmc.local",
        "http://openmce.local:8080",
        "http://openmce.local",
        "http://data.tvdownload.microsoft.com:8080",
        "http://data.tvdownload.microsoft.com",
        "http://mediacenter:8080",
        "http://wmc:8080"
    )

    # Also probe any configured IPv4 DNS server IP addresses
    try {
        $dnsList = (Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue).ServerAddresses | Select-Object -Unique
        foreach ($ip in $dnsList) {
            if ($ip -and $ip -ne "127.0.0.1" -and $ip -notmatch "^169\.254\.") {
                $candidates += "http://${ip}:8080"
                $candidates += "http://${ip}"
            }
        }
    } catch {}

    foreach ($cand in $candidates) {
        try {
            $resp = Invoke-RestMethod -Uri "$cand/health" -TimeoutSec 2 -ErrorAction Stop
            if ($resp.status -eq "healthy" -or $resp.version) {
                Log-Message "Auto-discovered server at: $cand (v$($resp.version))" "SUCCESS"
                return $cand
            }
        } catch {}
    }

    Log-Message "Could not auto-discover server. Falling back to: $DefaultUrl" "WARN"
    return $DefaultUrl
}

$ServerUrl = Discover-Server -DefaultUrl $ServerUrl
Log-Message "Active Server Endpoint: $ServerUrl"

$webClient = New-Object System.Net.WebClient
$webClient.Headers.Add("User-Agent", "MediaCenterServicePack-Client/1.0")

# Locate eHome utilities (loadmxf.exe, ehPrivJob.exe)
$ehomeCandidates = @(
    "$env:SystemRoot\ehome",
    "$env:SystemRoot\SysWOW64\ehome",
    "$env:SystemDrive\Windows\ehome"
)
$loadMxfPath = $null
$ehPrivJob = $null
$ehomeDir = $null
foreach ($dir in $ehomeCandidates) {
    if (Test-Path (Join-Path $dir "loadmxf.exe")) {
        $loadMxfPath = Join-Path $dir "loadmxf.exe"
        $ehomeDir = $dir
    }
    if (Test-Path (Join-Path $dir "ehPrivJob.exe")) {
        $ehPrivJob = Join-Path $dir "ehPrivJob.exe"
        $ehomeDir = $dir
    }
    if ($loadMxfPath -or $ehPrivJob) { break }
}

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

# 4. Direct Music Artwork Download (Single Album)
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
        if (-not (Test-Path $wmcArtCache)) { New-Item -ItemType Directory -Path $wmcArtCache -Force | Out-Null }
        Copy-Item -Path (Join-Path $albumOutDir "Folder.jpg") -Destination $wmcArtCache -Force -ErrorAction SilentlyContinue
        Log-Message "Copied 500x500 artwork into WMC art_cache." "SUCCESS"

        # Trigger re-indexing
        if ($ehPrivJob -and (Test-Path $ehPrivJob)) {
            Start-Process -FilePath $ehPrivJob -ArgumentList "/DoReIndexing" -Wait -NoNewWindow -ErrorAction SilentlyContinue
            Start-Process -FilePath $ehPrivJob -ArgumentList "/UpdateDiscMetadata" -Wait -NoNewWindow -ErrorAction SilentlyContinue
        }
    } catch {
        Log-Message "Failed downloading music artwork: $_" "ERROR"
    }
}

# 5. Batch Music Folder Artwork Enrichment (Optional)
if ($MusicFolder) {
    if (-not (Test-Path $MusicFolder)) {
        Log-Message "Specified MusicFolder does not exist: $MusicFolder" "ERROR"
    } else {
        Log-Message "Scanning MusicFolder for albums missing artwork: $MusicFolder"
        $subDirs = Get-ChildItem -Path $MusicFolder -Directory -Recurse -ErrorAction SilentlyContinue
        $albumDirs = @()
        foreach ($d in $subDirs) {
            $hasAudio = Get-ChildItem -Path $d.FullName -Include "*.mp3","*.wma","*.flac","*.m4a","*.aac" -File -Depth 1 -ErrorAction SilentlyContinue
            if ($hasAudio) {
                $albumDirs += $d
            }
        }
        $rootAudio = Get-ChildItem -Path $MusicFolder -Include "*.mp3","*.wma","*.flac","*.m4a","*.aac" -File -Depth 1 -ErrorAction SilentlyContinue
        if ($rootAudio) {
            $albumDirs += (Get-Item $MusicFolder)
        }

        Log-Message "Found $($albumDirs.Count) music album directories."
        $wmcArtCache = "$env:LOCALAPPDATA\Microsoft\eHome\art_cache"
        if (-not (Test-Path $wmcArtCache)) { New-Item -ItemType Directory -Path $wmcArtCache -Force | Out-Null }

        $downloadedCount = 0
        foreach ($dir in $albumDirs) {
            $folderJpg = Join-Path $dir.FullName "Folder.jpg"
            if (-not (Test-Path $folderJpg)) {
                $cleanName = $dir.Name -replace '^\d+\s*[-.]\s*', '' -replace '^\(\d{4}\)\s*', ''
                Log-Message "Fetching artwork for album: '$cleanName'..."
                try {
                    $tmpZip = Join-Path $WorkingDirectory "temp_art.zip"
                    $url = "$($ServerUrl.TrimEnd('/'))/api/metadata/music/$([System.Uri]::EscapeDataString($cleanName))/download"
                    $webClient.DownloadFile($url, $tmpZip)

                    $archive = [System.IO.Compression.ZipFile]::OpenRead($tmpZip)
                    foreach ($entry in $archive.Entries) {
                        if ($entry.Name -in @("Folder.jpg", "folder.jpg", "AlbumArtSmall.jpg")) {
                            $targetFile = Join-Path $dir.FullName $entry.Name
                            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $targetFile, $true)
                        }
                    }
                    $archive.Dispose()
                    Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue

                    if (Test-Path $folderJpg) {
                        Copy-Item -Path $folderJpg -Destination $wmcArtCache -Force -ErrorAction SilentlyContinue
                    }
                    $downloadedCount++
                    Log-Message "Added WMC artwork to: $($dir.FullName)" "SUCCESS"
                } catch {
                    Log-Message "Could not find artwork for '$cleanName': $_" "WARN"
                }
            } else {
                Copy-Item -Path $folderJpg -Destination $wmcArtCache -Force -ErrorAction SilentlyContinue
            }
        }
        Log-Message "Completed music folder enrichment. Downloaded artwork for $downloadedCount albums." "SUCCESS"
        $RefreshMusic = $true
    }
}

# 6. Refresh / Reset Music Library & Database (Optional)
if ($ResetMusicDatabase) {
    Log-Message "Resetting Windows Media Player & Media Center music library database..." "WARN"
    Stop-Service -Name "wmpnetwk" -Force -ErrorAction SilentlyContinue
    Stop-Process -Name "ehshell", "wmplayer" -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    $wmpDbPath = "$env:LOCALAPPDATA\Microsoft\Media Player"
    if (Test-Path $wmpDbPath) {
        Get-ChildItem -Path $wmpDbPath -Filter "CurrentDatabase_*.wmdb" -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
        Log-Message "Cleared WMP CurrentDatabase_*.wmdb library database." "SUCCESS"
    }

    $wmcArtCache = "$env:LOCALAPPDATA\Microsoft\eHome\art_cache"
    if (Test-Path $wmcArtCache) {
        Remove-Item "$wmcArtCache\*" -Recurse -Force -ErrorAction SilentlyContinue
        Log-Message "Cleared eHome art_cache." "SUCCESS"
    }

    Start-Service -Name "wmpnetwk" -ErrorAction SilentlyContinue
    $RefreshMusic = $true
}

if ($RefreshMusic) {
    Log-Message "Triggering Windows Media Center eHome music re-indexing..."
    if ($ehPrivJob -and (Test-Path $ehPrivJob)) {
        Start-Process -FilePath $ehPrivJob -ArgumentList "/DoReIndexing" -Wait -NoNewWindow -ErrorAction SilentlyContinue
        Start-Process -FilePath $ehPrivJob -ArgumentList "/UpdateDiscMetadata" -Wait -NoNewWindow -ErrorAction SilentlyContinue
        Log-Message "ehPrivJob /DoReIndexing and /UpdateDiscMetadata completed." "SUCCESS"
    } else {
        Log-Message "Notice: ehPrivJob.exe not found. Restart Windows Media Center to view updated music metadata." "WARN"
    }
}

# 7. Guide Ingestion Routine (Skip if running specific media/music operations only)
$hasMediaOp = $DownloadMovie -or $DownloadAlbum -or $MusicFolder -or $RefreshMusic -or $ResetMusicDatabase
if (-not $hasMediaOp -or $SyncNow) {
    if (-not $loadMxfPath) {
        Log-Message "Could not locate loadmxf.exe. Is Windows Media Center installed?" "WARN"
    } else {
        # Trigger EPG sync on backend if API key or SyncNow was provided
        if ($ApiKey -or $SyncNow) {
            $syncEndpoint = "$($ServerUrl.TrimEnd('/'))/api/epg/sync"
            if ($ApiKey) {
                $syncEndpoint += "?api_key=$([System.Uri]::EscapeDataString($ApiKey))"
            }
            Log-Message "Triggering backend EPG sync with service API key..."
            try {
                $syncResp = Invoke-RestMethod -Uri $syncEndpoint -Method Post
                Log-Message "Backend sync successful: $($syncResp.message)" "SUCCESS"
            } catch {
                Log-Message "Notice: Backend sync trigger reported: $_" "WARN"
            }
        }

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

        if ($ehPrivJob -and (Test-Path $ehPrivJob)) {
            Log-Message "Refreshing eHome caches via ehPrivJob..."
            Start-Process -FilePath $ehPrivJob -ArgumentList "/DoReIndexing" -Wait -NoNewWindow -ErrorAction SilentlyContinue
            Start-Process -FilePath $ehPrivJob -ArgumentList "/UpdateDiscMetadata" -Wait -NoNewWindow -ErrorAction SilentlyContinue
        }
    }
}

Log-Message "=== Task Complete! ===" "SUCCESS"
exit 0
