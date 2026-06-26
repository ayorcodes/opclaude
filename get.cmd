@echo off
:: Bootstrap for Windows — no PowerShell execution policy issues.
:: Usage: download and double-click, or run from cmd:
::   curl -L https://raw.githubusercontent.com/ayorcodes/opclaude/main/get.cmd -o "%TEMP%\opclaude-get.cmd" && "%TEMP%\opclaude-get.cmd"
setlocal

set SRC_DIR=%USERPROFILE%\.opclaude-src
if defined OPCLAUDE_SRC_DIR set SRC_DIR=%OPCLAUDE_SRC_DIR%

where git >nul 2>&1
if errorlevel 1 (
    echo git is required. Install it with: winget install Git.Git
    exit /b 1
)

if exist "%SRC_DIR%\.git" (
    echo Updating existing checkout at %SRC_DIR% ...
    git -C "%SRC_DIR%" pull --ff-only
) else (
    echo Cloning opclaude into %SRC_DIR% ...
    git clone https://github.com/ayorcodes/opclaude "%SRC_DIR%"
    if errorlevel 1 exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
    echo Node.js not found. Installing via winget...
    winget install --id OpenJS.NodeJS -e --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo Failed to install Node.js. Install it manually from https://nodejs.org and re-run.
        exit /b 1
    )
    echo Node.js installed. Please open a new terminal and re-run this script.
    exit /b 0
)

node "%SRC_DIR%\install.js"
endlocal
