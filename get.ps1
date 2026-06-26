# Bootstrap for Windows: clones opclaude and runs install.ps1.
#
# Usage (run in an elevated or normal PowerShell prompt):
#   iwr -useb https://raw.githubusercontent.com/ayorcodes/opclaude/main/get.ps1 -OutFile "$env:TEMP\opclaude-get.ps1"; & "$env:TEMP\opclaude-get.ps1"
#
# NOTE: Do NOT use `iwr ... | iex` — that breaks interactive prompts in
# install.ps1. Download first, then execute.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$REPO_URL = "https://github.com/ayorcodes/opclaude"
$SRC_DIR  = if ($env:OPCLAUDE_SRC_DIR) { $env:OPCLAUDE_SRC_DIR } else { "$env:USERPROFILE\.opclaude-src" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "git is required. Install it with: winget install Git.Git"
    exit 1
}

if (Test-Path "$SRC_DIR\.git") {
    Write-Host "Updating existing checkout at $SRC_DIR ..."
    git -C $SRC_DIR pull --ff-only
} else {
    Write-Host "Cloning opclaude into $SRC_DIR ..."
    git clone $REPO_URL $SRC_DIR
}

& "$SRC_DIR\install.ps1"
