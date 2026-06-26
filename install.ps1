# Sets up opclaude on Windows: installs uv, Node.js, Claude Code, litellm,
# applies patches, saves secrets, and wires up the bin wrappers.
#
# Usage: .\install.ps1
#   (or via get.ps1 bootstrap)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$REPO_DIR         = Split-Path -Parent $MyInvocation.MyCommand.Path
$STATE_DIR        = "$env:USERPROFILE\.config\opclaude"
$ENV_FILE         = "$STATE_DIR\.env"
$BIN_DIR          = "$env:USERPROFILE\.local\bin"
$LITELLM_VERSION  = "1.89.3"

Write-Host "== opclaude install (Windows) =="
New-Item -ItemType Directory -Force -Path $STATE_DIR | Out-Null
New-Item -ItemType Directory -Force -Path $BIN_DIR   | Out-Null

# --- winget availability check -------------------------------------------
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Error "winget not found. Update Windows to 1809+ or install App Installer from the Microsoft Store."
    exit 1
}

# --- uv ------------------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "uv (the Python tool installer litellm runs under) is not installed."
    $reply = Read-Host "Install it now via winget? [Y/n]"
    if ($reply -match '^[Nn]') {
        Write-Error "Install uv yourself (https://docs.astral.sh/uv/) and re-run this script."
        exit 1
    }
    winget install --id astral-sh.uv -e --accept-package-agreements --accept-source-agreements
    # Refresh PATH for this session
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH", "User")
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Error "uv installed but not found on PATH yet. Open a new terminal and re-run this script."
        exit 1
    }
}

# --- Node.js + Claude Code -----------------------------------------------
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host ""
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Host "Node.js (npm) is not installed."
        $reply = Read-Host "Install Node.js via winget? [Y/n]"
        if ($reply -match '^[Nn]') {
            Write-Error "Install Node.js yourself (https://nodejs.org) and re-run this script."
            exit 1
        }
        winget install --id OpenJS.NodeJS -e --accept-package-agreements --accept-source-agreements
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("PATH", "User")
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            Write-Error "npm installed but not found on PATH yet. Open a new terminal and re-run this script."
            exit 1
        }
    }

    Write-Host "Claude Code CLI ('claude') is not installed."
    $reply = Read-Host "Install it now via npm? [Y/n]"
    if ($reply -match '^[Nn]') {
        Write-Error "Install Claude Code yourself (npm install -g @anthropic-ai/claude-code) and re-run this script."
        exit 1
    }
    npm install -g @anthropic-ai/claude-code
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH", "User")
    if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
        Write-Error "claude installed but not found on PATH yet. Open a new terminal and re-run this script."
        exit 1
    }
}

# --- litellm + patch ------------------------------------------------------
Write-Host ""
Write-Host "Installing litellm $LITELLM_VERSION via uv ..."
uv tool install "litellm==$LITELLM_VERSION" --force --with "litellm[proxy,extra-proxy]"

Write-Host "Applying our patch for litellm bug #2 (see FIX.md) ..."
& "$REPO_DIR\patches\apply.ps1"

# --- secrets --------------------------------------------------------------
$OPENCODE_API_KEY  = ""
$LITELLM_MASTER_KEY = ""

if (Test-Path $ENV_FILE) {
    Get-Content $ENV_FILE | ForEach-Object {
        if ($_ -match '^OPENCODE_API_KEY=(.+)$')  { $OPENCODE_API_KEY  = $Matches[1] }
        if ($_ -match '^LITELLM_MASTER_KEY=(.+)$') { $LITELLM_MASTER_KEY = $Matches[1] }
    }
}

if (-not $OPENCODE_API_KEY) {
    Write-Host ""
    $OPENCODE_API_KEY = Read-Host -AsSecureString "Enter your opencode API key (OPENCODE_API_KEY)"
    $OPENCODE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($OPENCODE_API_KEY))
    if (-not $OPENCODE_API_KEY) {
        Write-Error "An opencode API key is required (https://opencode.ai — needs a Go subscription)."
        exit 1
    }
}

if (-not $LITELLM_MASTER_KEY) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RNGCryptoServiceProvider]::Create().GetBytes($bytes)
    $hex = ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
    $LITELLM_MASTER_KEY = "sk-$hex"
}

$envContent = "OPENCODE_API_KEY=$OPENCODE_API_KEY`nLITELLM_MASTER_KEY=$LITELLM_MASTER_KEY"
[System.IO.File]::WriteAllText($ENV_FILE, $envContent, [System.Text.Encoding]::UTF8)
(Get-Item $ENV_FILE).Attributes = [System.IO.FileAttributes]::Normal
Write-Host "Saved secrets to $ENV_FILE"

# --- write .cmd shims into BIN_DIR ----------------------------------------
# Each .cmd shim just delegates to the matching .ps1 in the repo's bin/.
$shims = @("opclaude", "opclaude-proxy", "oc", "oc-classify")
foreach ($name in $shims) {
    $ps1 = "$REPO_DIR\bin\$name.ps1"
    $cmd = "$BIN_DIR\$name.cmd"

    if ($name -eq "oc-classify") {
        # oc-classify is a Python script, not a .ps1
        $cmdContent = "@echo off`r`nuv run python `"$REPO_DIR\bin\oc-classify`" %*`r`n"
    } else {
        $cmdContent = "@echo off`r`npowershell -ExecutionPolicy Bypass -File `"$ps1`" %*`r`n"
    }
    [System.IO.File]::WriteAllText($cmd, $cmdContent, [System.Text.Encoding]::ASCII)
}
Write-Host "Wrote opclaude, opclaude-proxy, oc, oc-classify shims into $BIN_DIR"

# --- router config (for `oc`) --------------------------------------------
$ROUTER_CONFIG = "$STATE_DIR\router.yaml"
if (-not (Test-Path $ROUTER_CONFIG)) {
    Copy-Item "$REPO_DIR\router.yaml.example" $ROUTER_CONFIG
    Write-Host "Seeded $ROUTER_CONFIG from router.yaml.example."
}

# --- PATH -----------------------------------------------------------------
$userPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
if ($userPath -notlike "*$BIN_DIR*") {
    [System.Environment]::SetEnvironmentVariable("PATH", "$BIN_DIR;$userPath", "User")
    $env:PATH = "$BIN_DIR;$env:PATH"
    Write-Host ""
    Write-Host "Added $BIN_DIR to your user PATH."
    Write-Host "Open a new terminal window for the change to take effect in all apps."
}

Write-Host ""
Write-Host "Done. Run 'opclaude' to start Claude Code routed through opencode models."
Write-Host "Manage the background proxy with: opclaude-proxy start|stop|restart|status"
