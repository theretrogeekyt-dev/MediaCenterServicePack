<#
.SYNOPSIS
    MediaCenterServicePack - Lightweight Client Synchronization for Windows Media Center.
.DESCRIPTION
    100% Windows 7 SP1 (PowerShell 2.0 / .NET 3.5) compatible.
    Downloads compiled guide.mxf and artwork from the MediaCenterServicePack server,
    ingests via Microsoft's native loadmxf.exe, auto-maps tuner channels, and
    schedules daily background synchronization.
#>

[CmdletBinding()]
param (
    [Parameter(Mandatory = $false)]
    [string]$ServerUrl = "http://mediacenter.local:8080",

    [Parameter(Mandatory = $false)]
    [string]$WorkingDirectory = "$env:ProgramData\MediaCenterServicePack",

    [Parameter(Mandatory = $false)]
    [switch]$Setup = $false,

    [Parameter(Mandatory = $false)]
    [switch]$InstallTask = $false,

    [Parameter(Mandatory = $false)]
    [switch]$UninstallTask = $false,

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
            "ERROR"   { Write-Host $formatted -ForegroundColor Red }
            "WARN"    { Write-Host $formatted -ForegroundColor Yellow }
            "SUCCESS" { Write-Host $formatted -ForegroundColor Green }
            default   { Write-Host $formatted -ForegroundColor Cyan }
        }
    }
    if ($WorkingDirectory) {
        try {
            Add-Content -Path (Join-Path $WorkingDirectory "sync-wmc.log") -Value $formatted -ErrorAction SilentlyContinue
        } catch {}
    }
}

Log-Message "=== MediaCenterServicePack Client Synchronization ==="

# 1. Ensure working directory
if (-not (Test-Path $WorkingDirectory)) {
    New-Item -ItemType Directory -Path $WorkingDirectory -Force | Out-Null
}

# 2. Locate native Windows Media Center binaries (ehome directory)
$ehomeCandidates = @(
    "$env:SystemDrive\Windows\ehome",
    "$env:ProgramFiles\Windows Media Center\ehome",
    "${env:ProgramFiles(x86)}\Windows Media Center\ehome"
)
$ehomeDir = $null
$loadMxfPath = $null
foreach ($dir in $ehomeCandidates) {
    if (Test-Path (Join-Path $dir "loadmxf.exe")) {
        $loadMxfPath = Join-Path $dir "loadmxf.exe"
        $ehomeDir = $dir
        break
    }
}

if (-not $loadMxfPath) {
    Log-Message "loadmxf.exe not found. Is Windows Media Center installed on this machine?" "ERROR"
    exit 1
}

# 3. Handle Task Uninstallation
if ($UninstallTask) {
    Log-Message "Removing scheduled background tasks and shortcuts..."
    Start-Process -FilePath "schtasks.exe" -ArgumentList "/delete /tn `"MediaCenterServicePack_AutoSync`" /f" -Wait -NoNewWindow -ErrorAction SilentlyContinue
    Start-Process -FilePath "schtasks.exe" -ArgumentList "/delete /tn `"MediaCenterServicePack_LogonSync`" /f" -Wait -NoNewWindow -ErrorAction SilentlyContinue
    $desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Sync Windows Media Center.lnk"
    if (Test-Path $desktopShortcut) { Remove-Item $desktopShortcut -Force -ErrorAction SilentlyContinue }
    Log-Message "Uninstalled tasks and shortcuts." "SUCCESS"
    exit 0
}

# 4. Handle Task & Shortcut Installation (-InstallTask or -Setup)
if ($InstallTask -or $Setup) {
    Log-Message "Installing automated Windows Media Center background tasks..."
    $installedScript = Join-Path $WorkingDirectory "sync-wmc.ps1"
    if ($PSCommandPath -and (Test-Path $PSCommandPath) -and ($PSCommandPath -ne $installedScript)) {
        Copy-Item -Path $PSCommandPath -Destination $installedScript -Force
    }

    # Silent VBS launcher (runs PowerShell in background without flashing a black console window)
    $vbsPath = Join-Path $WorkingDirectory "run-hidden.vbs"
    $vbsCode = "Set WshShell = CreateObject(""WScript.Shell"")`r`nWshShell.Run ""powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File """"$installedScript"""" -Quiet"", 0, False`r`n"
    try {
        if (-not (Test-Path $vbsPath) -or [System.IO.File]::ReadAllText($vbsPath) -ne $vbsCode) {
            [System.IO.File]::WriteAllText($vbsPath, $vbsCode, [System.Text.Encoding]::ASCII)
        }
    } catch {}

    # Register Daily 4:00 AM sync task and Logon sync task (standard Windows schtasks.exe)
    Start-Process -FilePath "schtasks.exe" -ArgumentList "/create /tn `"MediaCenterServicePack_AutoSync`" /tr `"wscript.exe `\`"$vbsPath`\`"`" /sc daily /st 04:00 /rl HIGHEST /f" -Wait -NoNewWindow
    Start-Process -FilePath "schtasks.exe" -ArgumentList "/create /tn `"MediaCenterServicePack_LogonSync`" /tr `"wscript.exe `\`"$vbsPath`\`"`" /sc onlogon /rl HIGHEST /f" -Wait -NoNewWindow

    # Create Desktop Shortcut
    try {
        $desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Sync Windows Media Center.lnk"
        $wsh = New-Object -ComObject WScript.Shell
        $sc = $wsh.CreateShortcut($desktopShortcut)
        $sc.TargetPath = "wscript.exe"
        $sc.Arguments = "`"$vbsPath`""
        $sc.Description = "Update TV Guide listings and artwork for Windows Media Center"
        if ($ehomeDir -and (Test-Path (Join-Path $ehomeDir "ehshell.exe"))) {
            $sc.IconLocation = (Join-Path $ehomeDir "ehshell.exe") + ",0"
        }
        $sc.Save()
        Log-Message "Created desktop shortcut: $desktopShortcut" "SUCCESS"
    } catch {}

    # Register native WMC Start Menu application tile via RegisterMCEApp.exe
    try {
        $regMceApp = Join-Path $ehomeDir "RegisterMCEApp.exe"
        if (Test-Path $regMceApp) {
            $appXmlPath = Join-Path $WorkingDirectory "appInfo.xml"
            $appXml = @"
<application title="MediaCenterServicePack" id="{b7f43da2-2b63-4a11-a8cf-30045610214a}">
    <entrypoint id="{a1c43da2-1b63-4a11-a8cf-30045610214b}"
                title="Service Pack Status &amp; Sync"
                description="Update TV Guide, TV series artwork, and movie metadata"
                run="wscript.exe"
                args="$vbsPath"
                category="More Programs">
    </entrypoint>
</application>
"@
            try {
                if (-not (Test-Path $appXmlPath) -or [System.IO.File]::ReadAllText($appXmlPath) -ne $appXml) {
                    [System.IO.File]::WriteAllText($appXmlPath, $appXml)
                }
            } catch {}
            Start-Process -FilePath $regMceApp -ArgumentList @("/u", "/allusers", $appXmlPath) -Wait -NoNewWindow -ErrorAction SilentlyContinue
            Start-Process -FilePath $regMceApp -ArgumentList @("/allusers", $appXmlPath) -Wait -NoNewWindow -ErrorAction SilentlyContinue
            Log-Message "Registered native WMC Start Menu entrypoint tile." "SUCCESS"
        }
    } catch {}

    if ($InstallTask -and -not $Setup) {
        Log-Message "Installation complete." "SUCCESS"
        exit 0
    }
}

# 5. Server Discovery & Health Check (PowerShell 2.0 WebClient compatible)
$wc = New-Object System.Net.WebClient
$wc.Headers.Add("User-Agent", "MediaCenterServicePack-Client/1.0")

function Check-Server {
    param([string]$Url)
    try {
        $testUrl = "$($Url.TrimEnd('/'))/health"
        $resp = $wc.DownloadString($testUrl)
        if ($resp -match '"healthy"' -or $resp -match '"version"') { return $true }
    } catch {}
    return $false
}

$activeServer = $ServerUrl
if (-not (Check-Server -Url $activeServer)) {
    $candidates = @("http://mediacenter.local:8080", "http://mediacenter.local", "http://127.0.0.1:8080")
    foreach ($cand in $candidates) {
        if (Check-Server -Url $cand) {
            $activeServer = $cand
            break
        }
    }
}
Log-Message "Connected to MediaCenterServicePack server at: $activeServer"

# 6. Pre-cache Artwork Bundle if local storage is uninitialized
$imagesDir = Join-Path $WorkingDirectory "Images"
if (-not (Test-Path $imagesDir)) {
    New-Item -ItemType Directory -Path $imagesDir -Force | Out-Null
}

$existingArtCount = (Get-ChildItem -Path $imagesDir -File -ErrorAction SilentlyContinue).Count
if ($existingArtCount -lt 50) {
    Log-Message "Initial run detected. Downloading artwork bundle..."
    $bundleZip = Join-Path $WorkingDirectory "temp_bundle.zip"
    try {
        $wc.DownloadFile("$activeServer/api/client/bundle.zip", $bundleZip)
        if (Test-Path $bundleZip) {
            # Pure native COM unzipping (compatible with Windows XP/Vista/7/8/10/11 without .NET 4.5)
            $shell = New-Object -ComObject Shell.Application
            $zipPkg = $shell.NameSpace($bundleZip)
            $destFolder = $shell.NameSpace($imagesDir)
            $destFolder.CopyHere($zipPkg.Items(), 16)
            Remove-Item $bundleZip -Force -ErrorAction SilentlyContinue
            $extracted = (Get-ChildItem -Path $imagesDir -File -ErrorAction SilentlyContinue).Count
            Log-Message "Extracted $extracted artwork assets into local cache." "SUCCESS"
        }
    } catch {
        Log-Message "Notice: Artwork bundle skipped ($_); will stream as needed." "WARN"
    }
}

# 7. Download & Ingest TV Guide Payload (guide.mxf)
$mxfPath = Join-Path $WorkingDirectory "guide.mxf"
Log-Message "Downloading latest TV Guide from $activeServer/api/guide.mxf..."
try {
    $wc.DownloadFile("$activeServer/api/guide.mxf", $mxfPath)
} catch {
    Log-Message "Failed to download guide.mxf: $_" "ERROR"
    exit 2
}
$wc.Dispose()

# Temporarily halt WMC background recorder/scheduler during database write
if (-not $SkipServiceRestart) {
    Stop-Service -Name "ehsched" -Force -ErrorAction SilentlyContinue
    Stop-Service -Name "ehrecvr" -Force -ErrorAction SilentlyContinue
}

Log-Message "Ingesting guide payload via Microsoft loadmxf.exe..."
try {
    $proc = Start-Process -FilePath $loadMxfPath -ArgumentList "-i `"$mxfPath`"" -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -eq 0) {
        Log-Message "loadmxf.exe completed successfully (ExitCode: 0)." "SUCCESS"
    } else {
        Log-Message "loadmxf.exe finished with ExitCode: $($proc.ExitCode)" "WARN"
    }
} catch {
    Log-Message "Error executing loadmxf.exe: $_" "ERROR"
}

# 8. Auto-Map Scanned Tuner Channels & Hide Dead Channels
function Map-WmcChannels {
    param([string]$ehomeDir)
    try {
        $mcStoreDll = Join-Path $ehomeDir "McStore.dll"
        $ehRecObjDll = Join-Path $ehomeDir "ehRecObj.dll"
        $mcepgDll = Join-Path $ehomeDir "mcepg.dll"

        if (-not (Test-Path $mcStoreDll) -or -not (Test-Path $mcepgDll)) { return }

        Add-Type -Path $mcStoreDll -ErrorAction SilentlyContinue
        Add-Type -Path $ehRecObjDll -ErrorAction SilentlyContinue
        [System.GC]::Collect()
        [System.GC]::WaitForPendingFinalizers()

        $store = [Microsoft.MediaCenter.Store.ObjectStore]::Open($null, "Default", $null, $true)
        if (-not $store) { return }

        $channels = New-Object Microsoft.MediaCenter.Guide.Channels($store)
        $mergedChannels = New-Object Microsoft.MediaCenter.Guide.MergedChannels($store)

        # Index guide channels
        $guideMap = @{}
        foreach ($c in $channels) {
            if ($c.Lineup -and $c.Lineup.Name -eq "Local Broadcast Listings" -and $c.Service) {
                $numStr = $c.ChannelNumber.ToString()
                if (-not $guideMap.ContainsKey($numStr)) { $guideMap[$numStr] = $c }
            }
        }

        # Auto-map tuner channels
        $mappedCount = 0
        foreach ($mc in $mergedChannels) {
            $numStr = $mc.ChannelNumber.ToString()
            if ($guideMap.ContainsKey($numStr)) {
                $gc = $guideMap[$numStr]
                $orig = $mc.PrimaryChannel
                if ($mc.Service -eq $null -or $mc.Service.Id -ne $gc.Service.Id) {
                    $mc.PrimaryChannel = $gc
                    $mc.Service = $gc.Service
                    $mc.HasUserMappedListings = $true
                    if ($orig -and -not $mc.SecondaryChannels.Contains($orig)) {
                        $mc.AddSecondaryChannel($orig)
                    }
                    $mc.Update()
                    $mappedCount++
                }
            }
        }

        # Audit channel visibility & propagate logos
        $visibleCount = 0
        $hiddenCount = 0
        foreach ($mc in $mergedChannels) {
            if ($mc.CallSign -like "Deleted*") { continue }
            if ($mc.LogoImage -eq $null -and $mc.Service -and $mc.Service.LogoImage) {
                $mc.LogoImage = $mc.Service.LogoImage
                $mc.Update()
            }

            $hasListing = ($mc.Service -and $mc.Service.ScheduleEntries -and ($mc.Service.ScheduleEntries | Measure-Object).Count -gt 0)
            $hasLogo = ($mc.LogoImage -ne $null) -or ($mc.Service -and $mc.Service.LogoImage -ne $null)

            if (-not $hasListing -or -not $hasLogo) {
                if ($mc.UserBlockedState -ne [Microsoft.MediaCenter.Guide.UserBlockedState]::Disabled) {
                    $mc.UserBlockedState = [Microsoft.MediaCenter.Guide.UserBlockedState]::Disabled
                    $mc.Visibility = [Microsoft.MediaCenter.Guide.ChannelVisibility]::Blocked
                    $mc.Update()
                }
                $hiddenCount++
            } else {
                if ($mc.UserBlockedState -ne [Microsoft.MediaCenter.Guide.UserBlockedState]::Enabled) {
                    $mc.UserBlockedState = [Microsoft.MediaCenter.Guide.UserBlockedState]::Enabled
                    $mc.Visibility = [Microsoft.MediaCenter.Guide.ChannelVisibility]::Available
                    $mc.Update()
                }
                $visibleCount++
            }
        }

        $store.Dispose()
        Log-Message "Channel Audit: $visibleCount active broadcast channels visible, $hiddenCount unlisted/blank channels hidden." "SUCCESS"
    } catch {
        Log-Message "Notice during channel auto-mapping: $_" "WARN"
    }
}

Map-WmcChannels -ehomeDir $ehomeDir

# 9. Restart background services & trigger PVR re-index
if (-not $SkipServiceRestart) {
    Start-Service -Name "ehsched" -ErrorAction SilentlyContinue
    Start-Service -Name "ehrecvr" -ErrorAction SilentlyContinue
}

$mcupdate = Join-Path $ehomeDir "mcupdate.exe"
if (Test-Path $mcupdate) {
    Start-Process -FilePath $mcupdate -ArgumentList "-P" -Wait -NoNewWindow -ErrorAction SilentlyContinue
}

if ($Setup) {
    Log-Message "Setup complete! Launching Windows Media Center..." "SUCCESS"
    $ehshell = Join-Path $ehomeDir "ehshell.exe"
    if (Test-Path $ehshell) {
        Start-Process -FilePath $ehshell -ErrorAction SilentlyContinue
    }
}

Log-Message "=== Synchronization Complete! ===" "SUCCESS"
exit 0
