@echo off
setlocal EnableDelayedExpansion
title MediaCenterServicePack - 1-Click WMC Setup

echo ======================================================================
echo    MediaCenterServicePack - Zero-Terminal Windows Media Center Setup
echo ======================================================================
echo.

:: 1. Check for administrative privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Requesting administrative privileges...
    powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\"\"' -Verb RunAs"
    exit /b
)

echo [OK] Running with administrative privileges.
echo.

:: 2. Target installation directory
set "INSTALL_DIR=%ProgramData%\MediaCenterServicePack"
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

:: 3. Server URL configuration (fallback if DNS is not yet active)
set "SERVER_URL=__SERVER_URL__"
if "%SERVER_URL%"=="" set "SERVER_URL=http://localhost:8080"
if "%SERVER_URL%"=="__SERVER_URL__" set "SERVER_URL=http://localhost:8080"

echo [1/3] Installing MediaCenterServicePack automation files to %INSTALL_DIR%...

:: Copy sync-wmc.ps1 if present next to the batch file, or download from server
if exist "%~dp0sync-wmc.ps1" (
    copy /y "%~dp0sync-wmc.ps1" "%INSTALL_DIR%\sync-wmc.ps1" >nul
) else (
    echo [INFO] Downloading latest sync-wmc.ps1 from %SERVER_URL%...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { (New-Object System.Net.WebClient).DownloadFile('%SERVER_URL%/api/client/sync-wmc.ps1', '%INSTALL_DIR%\sync-wmc.ps1') } catch { exit 1 }"
)

:: 4. Run PowerShell automated task installer
echo [2/3] Configuring silent Windows background tasks and desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_DIR%\sync-wmc.ps1" -InstallTask -ServerUrl "%SERVER_URL%" -Quiet

:: 5. Trigger initial background update
echo [3/3] Triggering initial background sync for TV Guide, Music artwork, and Movie metadata...
if exist "%INSTALL_DIR%\run-hidden.vbs" (
    wscript.exe "%INSTALL_DIR%\run-hidden.vbs"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%INSTALL_DIR%\sync-wmc.ps1" -All -ServerUrl "%SERVER_URL%" -Quiet
)

echo.
echo ======================================================================
echo  SETUP COMPLETE!
echo.
echo  * TV Guide, Music Album Art, and Movie Metadata will now update
echo    automatically every day in the background.
echo  * A 'Sync Windows Media Center' shortcut has been added to your
echo    desktop for instant 1-click silent updates anytime.
echo  * You will never need to open a terminal or command prompt again!
echo ======================================================================
echo.

powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('MediaCenterServicePack has been successfully installed!`n`nYour TV Guide, Music Album Art, and Movie Metadata will now update automatically in the background with zero terminals required.`n`nA ''Sync Windows Media Center'' shortcut has also been placed on your Desktop.', 'MediaCenterServicePack Setup Complete', 0, 64)" >nul 2>&1
