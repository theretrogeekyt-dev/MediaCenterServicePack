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
    [switch]$All = $false,

    [Parameter(Mandatory = $false)]
    [string]$DownloadMovie = $null,

    [Parameter(Mandatory = $false)]
    [string]$DownloadAlbum = $null,

    [Parameter(Mandatory = $false)]
    [string]$MusicFolder = $null,

    [Parameter(Mandatory = $false)]
    [string]$MovieFolder = $null,

    [Parameter(Mandatory = $false)]
    [switch]$RefreshMusic = $false,

    [Parameter(Mandatory = $false)]
    [switch]$ResetMusicDatabase = $false,

    [Parameter(Mandatory = $false)]
    [switch]$InstallTask = $false,

    [Parameter(Mandatory = $false)]
    [switch]$UninstallTask = $false,

    [Parameter(Mandatory = $false)]
    [string]$ApiKey = $null,

    [Parameter(Mandatory = $false)]
    [switch]$SyncNow = $false,

    [Parameter(Mandatory = $false)]
    [switch]$SkipServiceRestart = $false,

    [Parameter(Mandatory = $false)]
    [string]$MxfFile = $null,

    [Parameter(Mandatory = $false)]
    [switch]$SkipDownload = $false,

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
    "$env:ProgramFiles\Windows Media Center\ehome",
    "${env:ProgramFiles(x86)}\Windows Media Center\ehome",
    "C:\Program Files\Windows Media Center\ehome",
    "C:\Program Files (x86)\Windows Media Center\ehome",
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

# Handle Task Installation
if ($InstallTask) {
    Log-Message "Installing automated Windows Media Center background tasks..."
    $targetDir = "$env:ProgramData\MediaCenterServicePack"
    if (-not (Test-Path $targetDir)) { New-Item -ItemType Directory -Path $targetDir -Force | Out-Null }

    $installedScript = Join-Path $targetDir "sync-wmc.ps1"
    Copy-Item -Path $PSCommandPath -Destination $installedScript -Force

    # Create run-hidden.vbs to launch PowerShell without any visible console window
    $vbsPath = Join-Path $targetDir "run-hidden.vbs"
    $vbsCode = "Set WshShell = CreateObject(""WScript.Shell"")`r`nWshShell.Run ""powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File """"$installedScript"""" -All -Quiet"", 0, False`r`n"
    [System.IO.File]::WriteAllText($vbsPath, $vbsCode, [System.Text.Encoding]::ASCII)

    # Register Daily Scheduled Task
    Start-Process -FilePath "schtasks.exe" -ArgumentList "/create /tn `"MediaCenterServicePack_AutoSync`" /tr `"wscript.exe `\`"$vbsPath`\`"`" /sc daily /st 04:00 /rl HIGHEST /f" -Wait -NoNewWindow
    Start-Process -FilePath "schtasks.exe" -ArgumentList "/create /tn `"MediaCenterServicePack_LogonSync`" /tr `"wscript.exe `\`"$vbsPath`\`"`" /sc onlogon /rl HIGHEST /f" -Wait -NoNewWindow

    # Create Desktop Shortcut
    try {
        $desktop = [Environment]::GetFolderPath("Desktop")
        $shortcutPath = Join-Path $desktop "Sync Windows Media Center.lnk"
        $wsh = New-Object -ComObject WScript.Shell
        $sc = $wsh.CreateShortcut($shortcutPath)
        $sc.TargetPath = "wscript.exe"
        $sc.Arguments = "`"$vbsPath`""
        $sc.Description = "Update TV Guide, Music Artwork, and Movie Metadata for Windows Media Center"
        if ($ehomeDir -and (Test-Path (Join-Path $ehomeDir "ehshell.exe"))) {
            $sc.IconLocation = (Join-Path $ehomeDir "ehshell.exe") + ",0"
        }
        $sc.Save()
        Log-Message "Created desktop shortcut: $shortcutPath" "SUCCESS"
    } catch {}

    Log-Message "MediaCenterServicePack automated background tasks installed successfully!" "SUCCESS"
    exit 0
}

if ($UninstallTask) {
    Log-Message "Removing MediaCenterServicePack background tasks..."
    Start-Process -FilePath "schtasks.exe" -ArgumentList "/delete /tn `"MediaCenterServicePack_AutoSync`" /f" -Wait -NoNewWindow -ErrorAction SilentlyContinue
    Start-Process -FilePath "schtasks.exe" -ArgumentList "/delete /tn `"MediaCenterServicePack_LogonSync`" /f" -Wait -NoNewWindow -ErrorAction SilentlyContinue
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "Sync Windows Media Center.lnk"
    if (Test-Path $shortcutPath) { Remove-Item $shortcutPath -Force -ErrorAction SilentlyContinue }
    Log-Message "Uninstalled tasks and shortcuts." "SUCCESS"
    exit 0
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

# Handle -All Default Folders
if ($All) {
    Log-Message "Mode: Full Automated Ingestion (-All). Processing TV Guide, Music Library, and Movie Library..."
    if (-not $MusicFolder) {
        $myMusic = [Environment]::GetFolderPath("MyMusic")
        if ($myMusic -and (Test-Path $myMusic)) { $MusicFolder = $myMusic }
        elseif (Test-Path "$env:USERPROFILE\Music") { $MusicFolder = "$env:USERPROFILE\Music" }
    }
    if (-not $MovieFolder) {
        $myVideos = [Environment]::GetFolderPath("MyVideos")
        if ($myVideos -and (Test-Path $myVideos)) { $MovieFolder = $myVideos }
        elseif (Test-Path "$env:USERPROFILE\Videos") { $MovieFolder = "$env:USERPROFILE\Videos" }
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
                    # Direct iTunes Search API fallback (requires zero server dependencies)
                    try {
                        $itunesQuery = [System.Uri]::EscapeDataString($cleanName)
                        $itunesUrl = "https://itunes.apple.com/search?term=$itunesQuery&entity=album&limit=1"
                        $itunesResp = Invoke-RestMethod -Uri $itunesUrl -TimeoutSec 10 -ErrorAction Stop
                        if ($itunesResp.results -and $itunesResp.results.Count -gt 0) {
                            $rawArtUrl = $itunesResp.results[0].artworkUrl100
                            if ($rawArtUrl) {
                                $hiResUrl = $rawArtUrl.Replace("100x100bb", "600x600bb")
                                $artWc = New-Object System.Net.WebClient
                                $artBytes = $artWc.DownloadData($hiResUrl)
                                $artWc.Dispose()
                                if ($artBytes -and $artBytes.Length -gt 500) {
                                    [System.IO.File]::WriteAllBytes((Join-Path $dir.FullName "Folder.jpg"), $artBytes)
                                    [System.IO.File]::WriteAllBytes((Join-Path $dir.FullName "folder.jpg"), $artBytes)
                                    [System.IO.File]::WriteAllBytes((Join-Path $dir.FullName "AlbumArtSmall.jpg"), $artBytes)
                                    Copy-Item -Path (Join-Path $dir.FullName "Folder.jpg") -Destination $wmcArtCache -Force -ErrorAction SilentlyContinue
                                    $downloadedCount++
                                    Log-Message "Enriched artwork from iTunes for: $($dir.FullName)" "SUCCESS"
                                    continue
                                }
                            }
                        }
                        Log-Message "Could not find artwork for '$cleanName': $_" "WARN"
                    } catch {
                        Log-Message "Could not find artwork for '$cleanName': $_" "WARN"
                    }
                }
            } else {
                Copy-Item -Path $folderJpg -Destination $wmcArtCache -Force -ErrorAction SilentlyContinue
            }
        }
        Log-Message "Completed music folder enrichment. Downloaded artwork for $downloadedCount albums." "SUCCESS"
        $RefreshMusic = $true
    }
}

# 6. Batch Movie Folder Enrichment (Optional or with -All)
if ($MovieFolder) {
    if (-not (Test-Path $MovieFolder)) {
        Log-Message "Specified MovieFolder does not exist: $MovieFolder" "ERROR"
    } else {
        Log-Message "Scanning MovieFolder for movies missing metadata: $MovieFolder"
        $subDirs = Get-ChildItem -Path $MovieFolder -Directory -Recurse -ErrorAction SilentlyContinue
        $movieDirs = @()
        foreach ($d in $subDirs) {
            $hasVideo = Get-ChildItem -Path $d.FullName -Include "*.mkv","*.mp4","*.avi","*.m2ts","*.iso","*.vob","*.wmv" -File -Depth 1 -ErrorAction SilentlyContinue
            if ($hasVideo) { $movieDirs += $d }
        }
        $rootVideo = Get-ChildItem -Path $MovieFolder -Include "*.mkv","*.mp4","*.avi","*.m2ts","*.iso","*.vob","*.wmv" -File -Depth 1 -ErrorAction SilentlyContinue
        if ($rootVideo) { $movieDirs += (Get-Item $MovieFolder) }

        Log-Message "Found $($movieDirs.Count) movie directories to inspect."
        $wmcDvdCache = "$env:LOCALAPPDATA\Microsoft\eHome\DvdInfoCache"
        if (-not (Test-Path $wmcDvdCache)) { New-Item -ItemType Directory -Path $wmcDvdCache -Force | Out-Null }

        $movieDownloadedCount = 0
        foreach ($dir in $movieDirs) {
            $dvdidXml = Join-Path $dir.FullName "dvdid.xml"
            $folderJpg = Join-Path $dir.FullName "folder.jpg"
            if (-not (Test-Path $dvdidXml) -or -not (Test-Path $folderJpg)) {
                $cleanName = $dir.Name -replace '^\d+\s*[-.]\s*', '' -replace '\(\d{4}\)', '' -replace '\[.*?\]', ''
                $cleanName = $cleanName.Trim()
                if ($cleanName) {
                    Log-Message "Fetching movie metadata for: '$cleanName'..."
                    try {
                        $tmpZip = Join-Path $WorkingDirectory "temp_movie.zip"
                        $url = "$($ServerUrl.TrimEnd('/'))/api/metadata/movies/$([System.Uri]::EscapeDataString($cleanName))/download"
                        $webClient.DownloadFile($url, $tmpZip)

                        $archive = [System.IO.Compression.ZipFile]::OpenRead($tmpZip)
                        foreach ($entry in $archive.Entries) {
                            $targetFile = Join-Path $dir.FullName $entry.Name
                            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $targetFile, $true)
                            if ($entry.Name.EndsWith(".xml", [System.StringComparison]::OrdinalIgnoreCase)) {
                                Copy-Item -Path $targetFile -Destination $wmcDvdCache -Force -ErrorAction SilentlyContinue
                            }
                        }
                        $archive.Dispose()
                        Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
                        $movieDownloadedCount++
                        Log-Message "Enriched movie metadata in: $($dir.FullName)" "SUCCESS"
                    } catch {
                        Log-Message "Could not find movie metadata for '$cleanName': $_" "WARN"
                    }
                }
            } else {
                Copy-Item -Path $dvdidXml -Destination $wmcDvdCache -Force -ErrorAction SilentlyContinue
            }
        }
        if ($movieDownloadedCount -gt 0) {
            Log-Message "Enriched $movieDownloadedCount movies with WMC metadata." "SUCCESS"
        }
    }
}

# 7. Refresh / Reset Music Library & Database (Optional)
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
    Log-Message "Refreshing Windows Media Player & Media Center music library..."
    $mcupdate = Join-Path $ehomeDir "mcupdate.exe"
    if ($mcupdate -and (Test-Path $mcupdate)) {
        Start-Process -FilePath $mcupdate -ArgumentList "-P" -Wait -NoNewWindow -ErrorAction SilentlyContinue
    }
}

# 8. Guide Ingestion Routine
$hasMediaOp = ($DownloadMovie -or $DownloadAlbum -or ($MusicFolder -and -not $All) -or ($MovieFolder -and -not $All) -or $RefreshMusic -or $ResetMusicDatabase) -and -not $All
if (-not $hasMediaOp -or $SyncNow -or $All) {
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

        $mxfLocalPath = if ($MxfFile -and (Test-Path $MxfFile)) { $MxfFile } else { Join-Path $WorkingDirectory "guide.mxf" }
        if (-not $SkipDownload -and -not $MxfFile) {
            $mxfEndpoint = "$($ServerUrl.TrimEnd('/'))/api/guide.mxf"
            Log-Message "Downloading guide payload from: $mxfEndpoint"

            try {
                $webClient.DownloadFile($mxfEndpoint, $mxfLocalPath)
            } catch {
                Log-Message "Failed to download guide.mxf from $mxfEndpoint : $_" "ERROR"
                exit 3
            }
        } else {
            Log-Message "Using local guide payload: $mxfLocalPath" "INFO"
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

        # Auto-bind tuner scanned channels to guide listings
        function Map-WmcChannels {
            param([string]$ehomeDirectory)
            try {
                $mcStoreDll = Join-Path $ehomeDirectory "McStore.dll"
                $ehRecObjDll = Join-Path $ehomeDirectory "ehRecObj.dll"
                $mcepgDll = Join-Path $ehomeDirectory "mcepg.dll"

                if (-not (Test-Path $mcStoreDll) -or -not (Test-Path $mcepgDll)) {
                    Log-Message "Notice: McStore.dll or mcepg.dll not found in $ehomeDirectory. Skipping channel auto-mapping." "WARN"
                    return
                }

                Add-Type -Path $mcStoreDll -ErrorAction SilentlyContinue
                Add-Type -Path $ehRecObjDll -ErrorAction SilentlyContinue
                [System.GC]::Collect()
                [System.GC]::WaitForPendingFinalizers()
                Start-Sleep -Milliseconds 200

                $store = [Microsoft.MediaCenter.Store.ObjectStore]::Open($null, "Default", $null, $true)
                if (-not $store) { return }

                $channels = New-Object Microsoft.MediaCenter.Guide.Channels($store)
                $mergedChannels = New-Object Microsoft.MediaCenter.Guide.MergedChannels($store)

                # Index guide channels that have an assigned service
                $guideMap = @{}
                foreach ($c in $channels) {
                    if ($c.Lineup -and $c.Lineup.Name -eq "Local Broadcast Listings" -and $c.Service) {
                        $numStr = $c.ChannelNumber.ToString()
                        if (-not $guideMap.ContainsKey($numStr)) {
                            $guideMap[$numStr] = $c
                        }
                    }
                }

                $mappedCount = 0
                foreach ($mc in $mergedChannels) {
                    $mcNum = $mc.ChannelNumber.ToString()
                    if ($guideMap.ContainsKey($mcNum)) {
                        $gc = $guideMap[$mcNum]
                        $origPrimary = $mc.PrimaryChannel

                        if ($mc.Service -eq $null -or $mc.Service.Id -ne $gc.Service.Id) {
                            $mc.PrimaryChannel = $gc
                            $mc.Service = $gc.Service
                            $mc.HasUserMappedListings = $true

                            if ($origPrimary -and -not $mc.SecondaryChannels.Contains($origPrimary)) {
                                $mc.AddSecondaryChannel($origPrimary)
                            }
                            $mc.Update()
                            $mappedCount++
                        }
                    }
                }
                if ($mappedCount -gt 0) {
                    Log-Message "Automatically mapped $mappedCount tuner channels to Guide listings." "SUCCESS"
                } else {
                    Log-Message "All scanned tuner channels already mapped to Guide listings." "INFO"
                }

                # Propagate service logos to MergedChannels and prune channels lacking listings or logos
                $hiddenCount = 0
                $visibleCount = 0
                foreach ($mc in $mergedChannels) {
                    if ($mc.CallSign -like "Deleted*") { continue }

                    # Sync LogoImage from Service if missing
                    if ($mc.LogoImage -eq $null -and $mc.Service -and $mc.Service.LogoImage) {
                        $mc.LogoImage = $mc.Service.LogoImage
                        $mc.Update()
                    }

                    $schedCount = 0
                    if ($mc.Service -and $mc.Service.ScheduleEntries) {
                        foreach ($se in $mc.Service.ScheduleEntries) { $schedCount++ }
                    }
                    $hasLogo = ($mc.LogoImage -ne $null) -or ($mc.Service -and $mc.Service.LogoImage -ne $null)
                    $hasInfo = ($schedCount -gt 0)

                    if (-not $hasInfo -or -not $hasLogo) {
                        if ($mc.UserBlockedState -ne [Microsoft.MediaCenter.Guide.UserBlockedState]::Disabled -or $mc.Visibility -ne [Microsoft.MediaCenter.Guide.ChannelVisibility]::Blocked) {
                            $mc.UserBlockedState = [Microsoft.MediaCenter.Guide.UserBlockedState]::Disabled
                            $mc.Visibility = [Microsoft.MediaCenter.Guide.ChannelVisibility]::Blocked
                            $mc.Update()
                        }
                        $hiddenCount++
                    } else {
                        if ($mc.UserBlockedState -ne [Microsoft.MediaCenter.Guide.UserBlockedState]::Enabled -or $mc.Visibility -ne [Microsoft.MediaCenter.Guide.ChannelVisibility]::Available) {
                            $mc.UserBlockedState = [Microsoft.MediaCenter.Guide.UserBlockedState]::Enabled
                            $mc.Visibility = [Microsoft.MediaCenter.Guide.ChannelVisibility]::Available
                            $mc.Update()
                        }
                        $visibleCount++
                    }
                }
                Log-Message "Guide Channel Audit: $visibleCount channels active & visible, $hiddenCount dead/unlisted/logoless channels hidden." "SUCCESS"
                $store.Dispose()
            } catch {
                Log-Message "Channel auto-mapping notice: $_" "WARN"
            }
        }

        function Ensure-WmcRegion {
            param([string]$ehomeDirectory, [string]$PostalCode = "67147")
            try {
                $mcStoreDll = Join-Path $ehomeDirectory "McStore.dll"
                $mcepgDll = Join-Path $ehomeDirectory "mcepg.dll"

                if (-not (Test-Path $mcStoreDll) -or -not (Test-Path $mcepgDll)) {
                    return
                }

                Add-Type -Path $mcStoreDll -ErrorAction SilentlyContinue
                Add-Type -Path $mcepgDll -ErrorAction SilentlyContinue
                [System.GC]::Collect()
                [System.GC]::WaitForPendingFinalizers()
                Start-Sleep -Milliseconds 200

                $store = [Microsoft.MediaCenter.Store.ObjectStore]::Open($null, "Default", $null, $true)
                if (-not $store) { return }

                $regions = New-Object Microsoft.MediaCenter.Guide.Regions($store)
                $lineups = New-Object Microsoft.MediaCenter.Guide.Lineups($store)

                $lineup = $null
                foreach ($l in $lineups) {
                    if ($l.Name -eq "Local Broadcast Listings") {
                        $lineup = $l
                        break
                    }
                }
                if (-not $lineup) {
                    $store.Dispose()
                    return
                }

                # Ensure lineup is tagged with ATSC / Antenna types for Headend matching
                if (-not $lineup.LineupTypes -or -not $lineup.LineupTypes.Contains("ATSC")) {
                    $lineup.LineupTypes = "ATSC;Antenna;TER;Cable"
                    $lineup.AlternateLineupTypes = "ATSC;Antenna;TER;Cable"
                    $lineup.Update()
                }

                # Ensure lineup has !MCLineup UId for Headend subscription
                $hasMcUid = $false
                foreach ($u in $lineup.UIds) {
                    if ($u.GetFullName().StartsWith("!MCLineup")) {
                        $hasMcUid = $true
                        break
                    }
                }
                if (-not $hasMcUid) {
                    $uids = New-Object Microsoft.MediaCenter.Store.UIds($store)
                    $uids.Add("!MCLineup!USA-OTA-$PostalCode", $lineup)
                }

                # Clean up any corrupt or empty regions using ObjectCollectionDeleteRecords
                $regType = $regions.ObjectType
                $delMethod = $store.GetType().GetMethod("ObjectCollectionDeleteRecords", [System.Reflection.BindingFlags]"NonPublic,Public,Instance")
                $toDelete = @()
                foreach ($r in $regions) {
                    if (-not $r.Country -or -not $r.RegionCode -or ($r.Lineups | Measure-Object).Count -eq 0) {
                        $toDelete += $r.Id.ToString()
                    }
                }
                if ($toDelete.Count -gt 0 -and $delMethod) {
                    $delMethod.Invoke($store, @($regType, ($toDelete -join ",")))
                }

                # Verify or register RegionKey("US", $PostalCode)
                $key = New-Object Microsoft.MediaCenter.Guide.RegionKey("US", $PostalCode)
                $found = $regions[$key]
                if (-not $found -or ($found.Lineups | Measure-Object).Count -eq 0) {
                    $reg = New-Object Microsoft.MediaCenter.Guide.Region
                    $reg.Country = "US"
                    $reg.RegionCode = $PostalCode
                    $regions.Add($reg)
                    $reg.Lineups.Add($lineup)
                    $reg.Update()
                    Log-Message "Configured WMC guide Region for US $PostalCode." "SUCCESS"
                } else {
                    Log-Message "WMC guide Region for US $PostalCode is active and verified." "INFO"
                }

                # Register / update VideoDetails WebServiceLocator pointing to ServerUrl
                try {
                    $uids = New-Object Microsoft.MediaCenter.Store.UIds($store)
                    $vdUid = "!Microsoft.WindowsMedia.Services.Platform.Apps.Mdp.Services!VideoDetails"
                    $vdLoc = $uids[$vdUid]
                    $targetEndpoint = if ($ServerUrl) { $ServerUrl.TrimEnd('/') } else { "http://127.0.0.1:8080" }
                    if (-not $vdLoc -or -not $vdLoc.Target) {
                        $locColl = New-Object Microsoft.MediaCenter.Store.WebServiceLocators($store)
                        $newLoc = $locColl.ObjectType.New()
                        $newLoc.Url = $targetEndpoint
                        $newLoc.Protocol = "MDP"
                        $locColl.Add($newLoc)
                        try { $uids.Add($vdUid, $newLoc) } catch {}
                        $newLoc.Update()
                        Log-Message "Registered WMC Movie Details WebServiceLocator to $targetEndpoint" "SUCCESS"
                    } elseif ($vdLoc.Target.Url -ne $targetEndpoint) {
                        $vdLoc.Target.Url = $targetEndpoint
                        $vdLoc.Target.Update()
                    }
                } catch {
                    Log-Message "VideoDetails WebServiceLocator notice: $_" "WARN"
                }

                $store.Dispose()
            } catch {
                Log-Message "Region setup notice: $_" "WARN"
            }
        }

        function Import-WmcGuideListings {
            param(
                [string]$mxfPath,
                [string]$ehomeDirectory
            )
            try {
                $mcStoreDll = Join-Path $ehomeDirectory "McStore.dll"
                $mcepgDll = Join-Path $ehomeDirectory "mcepg.dll"

                if (-not (Test-Path $mcStoreDll) -or -not (Test-Path $mcepgDll)) {
                    Log-Message "Notice: McStore.dll or mcepg.dll not found in $ehomeDirectory. Skipping direct guide listings ingestion." "WARN"
                    return
                }

                Add-Type -Path $mcStoreDll -ErrorAction SilentlyContinue
                Add-Type -Path $mcepgDll -ErrorAction SilentlyContinue

                if (-not (Test-Path $mxfPath)) {
                    Log-Message "Notice: MXF guide file not found at $mxfPath" "WARN"
                    return
                }

                [xml]$xml = [System.IO.File]::ReadAllText($mxfPath)
                $providerName = "Schedules Direct"
                if ($xml.MXF.Providers.Provider.name) {
                    $providerName = $xml.MXF.Providers.Provider.name
                }

                $mxfServicesById = @{}
                foreach ($s in $xml.MXF.With.Services.Service) {
                    $mxfServicesById[$s.id] = $s
                }

                $mxfPrograms = @{}
                foreach ($p in $xml.MXF.With.Programs.Program) {
                    $mxfPrograms[$p.id] = $p
                }

                Log-Message "Directly ingesting $($mxfPrograms.Count) programs & listings under provider '$providerName'..."
                [System.GC]::Collect()
                [System.GC]::WaitForPendingFinalizers()
                Start-Sleep -Milliseconds 200

                $store = [Microsoft.MediaCenter.Store.ObjectStore]::Open($null, $providerName, $null, $true)
                if (-not $store) { return }

                $programs = New-Object Microsoft.MediaCenter.Guide.Programs($store)
                $progType = $programs.ObjectType
                $seColl = New-Object Microsoft.MediaCenter.Guide.ScheduleEntries($store)
                $seType = $seColl.ObjectType
                $uids = New-Object Microsoft.MediaCenter.Store.UIds($store)
                $delMethod = $store.GetType().GetMethod("ObjectCollectionDeleteRecords", [System.Reflection.BindingFlags]"NonPublic,Public,Instance")

                # 0. Index and instantiate GuideImages from MXF with local artwork caching
                $imgObjects = @{}
                $localImagesDir = Join-Path $WorkingDirectory "Images"
                if (-not (Test-Path $localImagesDir)) {
                    New-Item -ItemType Directory -Path $localImagesDir -Force | Out-Null
                }

                if ($xml.MXF.With.GuideImages -and $xml.MXF.With.GuideImages.GuideImage) {
                    $imagesColl = New-Object Microsoft.MediaCenter.Guide.GuideImages($store)
                    $imgType = $imagesColl.ObjectType

                    # Pre-cache un-cached artwork if server endpoint is available
                    $toDownload = @()
                    foreach ($giNode in $xml.MXF.With.GuideImages.GuideImage) {
                        if ($giNode.imageUrl -match "([a-f0-9]{64}\.jpg)") {
                            $hash = $matches[1]
                            $dest = Join-Path $localImagesDir $hash
                            if (-not (Test-Path $dest)) {
                                $toDownload += @{ Hash = $hash; Dest = $dest }
                            }
                        }
                    }

                    if ($toDownload.Count -gt 0 -and $ServerUrl) {
                        Log-Message "Pre-caching $($toDownload.Count) artwork images locally..."
                        $wc = New-Object System.Net.WebClient
                        $wc.Headers.Add("User-Agent", "MediaCenterServicePack-Client/1.0")
                        $dlCount = 0
                        foreach ($item in $toDownload) {
                            try {
                                $imgEndpoint = "$($ServerUrl.TrimEnd('/'))/api/images/$($item.Hash)"
                                $wc.DownloadFile($imgEndpoint, $item.Dest)
                                $dlCount++
                            } catch {}
                        }
                        $wc.Dispose()
                        if ($dlCount -gt 0) {
                            Log-Message "Pre-cached $dlCount artwork images to local storage." "SUCCESS"
                        }
                    }

                    foreach ($giNode in $xml.MXF.With.GuideImages.GuideImage) {
                        $imgUid = "!GuideImage!" + $giNode.id
                        $existingImg = $uids[$imgUid]
                        $gi = $null
                        if ($existingImg -and $existingImg.Target) {
                            $gi = $existingImg.Target
                        } else {
                            $gi = $imgType.New()
                            $imagesColl.Add($gi)
                            try { $uids.Add($imgUid, $gi) } catch {}
                        }

                        $assignedUrl = $giNode.imageUrl
                        if ($giNode.imageUrl -match "([a-f0-9]{64}\.jpg)") {
                            $hash = $matches[1]
                            $localFile = Join-Path $localImagesDir $hash
                            if (Test-Path $localFile) {
                                $assignedUrl = "file://" + ($localFile -replace '\\', '/')
                            }
                        }

                        if ($assignedUrl) { $gi.ImageUrl = $assignedUrl }
                        $gi.Update()
                        $imgObjects[$giNode.id] = $gi
                    }
                }

                # 1. Update/Instantiate Programs
                $progObjects = @{}
                foreach ($prId in $mxfPrograms.Keys) {
                    $pNode = $mxfPrograms[$prId]
                    $uidKey = "!Program!" + $prId
                    $existing = $uids[$uidKey]
                    $p = $null
                    if ($existing) {
                        $p = $existing.Target
                    } else {
                        $p = $progType.New()
                        $programs.Add($p)
                        try { $uids.Add($uidKey, $p) } catch {}
                    }

                    $p.Title = $pNode.title
                    if ($pNode.description) { $p.Description = $pNode.description }
                    if ($pNode.episodeTitle) { $p.EpisodeTitle = $pNode.episodeTitle }
                    if ($pNode.isSeries -eq "true") { $p.IsSeries = $true }
                    if ($pNode.isMovie -eq "true") { $p.IsMovie = $true }
                    if ($pNode.isNews -eq "true") { $p.IsNews = $true }
                    if ($pNode.isSports -eq "true") { $p.IsSports = $true }
                    if ($pNode.seasonNumber) {
                        $sn = 0
                        if ([int]::TryParse($pNode.seasonNumber, [ref]$sn)) { $p.SeasonNumber = $sn }
                    }
                    if ($pNode.episodeNumber) {
                        $en = 0
                        if ([int]::TryParse($pNode.episodeNumber, [ref]$en)) { $p.EpisodeNumber = $en }
                    }
                    if ($pNode.guideImage -and $imgObjects.ContainsKey($pNode.guideImage)) {
                        $p.GuideImage = $imgObjects[$pNode.guideImage]
                    }

                    $p.Update()
                    $progObjects[$prId] = $p
                }

                # 2. Map active broadcast Services from MergedChannels
                $mcs = New-Object Microsoft.MediaCenter.Guide.MergedChannels($store)
                $activeServicesByCall = @{}
                foreach ($mc in $mcs) {
                    if ($mc.CallSign -like "Deleted*") { continue }
                    if (-not $mc.Service) { continue }
                    $norm = ($mc.Service.CallSign -replace '[^\w]').ToUpper()
                    if (-not $activeServicesByCall.ContainsKey($norm)) {
                        $activeServicesByCall[$norm] = $mc.Service
                    }
                }

                # 2b. Assign station LogoImages to active broadcast Services
                foreach ($mxfSvcId in $mxfServicesById.Keys) {
                    $mxfSvc = $mxfServicesById[$mxfSvcId]
                    if ($mxfSvc.logoImage -and $imgObjects.ContainsKey($mxfSvc.logoImage)) {
                        $norm = ($mxfSvc.callSign -replace '[^\w]').ToUpper()
                        if ($activeServicesByCall.ContainsKey($norm)) {
                            $wmcSvc = $activeServicesByCall[$norm]
                            if ($wmcSvc.LogoImage -eq $null) {
                                $wmcSvc.LogoImage = $imgObjects[$mxfSvc.logoImage]
                                $wmcSvc.Update()
                            }
                        }
                    }
                }

                # 3. Clear existing schedule entries on active broadcast services
                $toDelSe = @()
                foreach ($call in $activeServicesByCall.Keys) {
                    $svc = $activeServicesByCall[$call]
                    foreach ($se in $svc.ScheduleEntries) {
                        $toDelSe += $se.Id.ToString()
                    }
                }
                if ($toDelSe.Count -gt 0 -and $delMethod) {
                    for ($i = 0; $i -lt $toDelSe.Count; $i += 500) {
                        $count = [Math]::Min(500, $toDelSe.Count - $i)
                        $batch = ($toDelSe[$i..($i + $count - 1)]) -join ","
                        $delMethod.Invoke($store, @($seType, $batch))
                    }
                }

                # 4. Inject ScheduleEntries into active broadcast services
                $totalInjected = 0
                $matchedCount = 0
                foreach ($seGroup in $xml.MXF.With.ScheduleEntries) {
                    $mxfSvcId = $seGroup.service
                    $mxfSvc = $mxfServicesById[$mxfSvcId]
                    if (-not $mxfSvc) { continue }

                    $norm = ($mxfSvc.callSign -replace '[^\w]').ToUpper()
                    if (-not $activeServicesByCall.ContainsKey($norm)) { continue }
                    $wmcSvc = $activeServicesByCall[$norm]
                    $matchedCount++

                    foreach ($entry in $seGroup.ScheduleEntry) {
                        $prId = $entry.program
                        $p = $progObjects[$prId]
                        if (-not $p) { continue }

                        $startTime = [DateTime]::Parse($entry.startTime, $null, [System.Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
                        $duration = [int]$entry.duration
                        $endTime = $startTime.AddSeconds($duration)

                        $se = $seType.New()
                        $se.Program = $p
                        $se.Service = $wmcSvc
                        $se.StartTime = $startTime
                        $se.EndTime = $endTime
                        $se.IsHdtv = ($entry.isHdtv -eq "true")
                        $se.IsRepeatFlag = ($entry.isRepeat -eq "true")
                        $se.IsCC = ($entry.isCc -eq "true")

                        $wmcSvc.ScheduleEntries.Add($se)
                        $totalInjected++
                    }
                }

                $store.Dispose()
                Log-Message "Injected $totalInjected live schedule entries across $matchedCount broadcast services into WMC Guide." "SUCCESS"
            } catch {
                Log-Message "Direct guide listings ingestion notice: $_" "WARN"
            }
        }

        Ensure-WmcRegion -ehomeDirectory $ehomeDir -PostalCode "67147"
        Map-WmcChannels -ehomeDirectory $ehomeDir
        Import-WmcGuideListings -mxfPath $mxfLocalPath -ehomeDirectory $ehomeDir
        Map-WmcChannels -ehomeDirectory $ehomeDir

        if (-not $SkipServiceRestart) {
            Log-Message "Restarting WMC services..."
            Start-Service -Name "ehsched" -ErrorAction SilentlyContinue
            Start-Service -Name "ehrecvr" -ErrorAction SilentlyContinue
        }

        $mcupdate = Join-Path $ehomeDir "mcupdate.exe"
        if ($mcupdate -and (Test-Path $mcupdate)) {
            Log-Message "Refreshing eHome PVR schedule tasks via mcupdate.exe..."
            Start-Process -FilePath $mcupdate -ArgumentList "-P" -Wait -NoNewWindow -ErrorAction SilentlyContinue
        }
    }
}

Log-Message "=== Task Complete! ===" "SUCCESS"
exit 0
