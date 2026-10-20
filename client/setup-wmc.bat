@echo off
setlocal EnableDelayedExpansion
title MediaCenterServicePack - 1-Go WMC Setup

echo ======================================================================
echo    MediaCenterServicePack - 1-Go Windows Media Center Setup
echo    Zero External Software - Automated Native Windows Setup
echo ======================================================================
echo.

:: 1. Check for administrative privileges and auto-elevate
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

:: 3. Server URL configuration (auto-discovery fallback)
set "SERVER_URL=__SERVER_URL__"
if "%SERVER_URL%"=="" set "SERVER_URL=http://mediacenter.local:8080"
if "%SERVER_URL%"=="__SERVER_URL__" set "SERVER_URL=http://mediacenter.local:8080"

:: 4. Download and execute 1-Go setup
echo [INFO] Launching 1-Go Windows Media Center setup...
if exist "%~dp0setup-wmc.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-wmc.ps1" -ServerUrl "%SERVER_URL%"
) else if exist "%INSTALL_DIR%\sync-wmc.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_DIR%\sync-wmc.ps1" -Setup -ServerUrl "%SERVER_URL%"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "iex ((New-Object System.Net.WebClient).DownloadString('%SERVER_URL%/setup.ps1'))"
)

exit /b 0
