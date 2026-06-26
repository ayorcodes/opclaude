# Run Claude Code through opencode Zen models via the local litellm proxy.
# Usage: opclaude [models | set-key | enable-ide | disable-ide | <claude args>]

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$REPO_DIR   = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$STATE_DIR  = "$env:USERPROFILE\.config\opclaude"
$ENV_FILE   = "$STATE_DIR\.env"
$SETTINGS   = "$env:USERPROFILE\.claude\settings.json"

# --- models ---------------------------------------------------------------
if ($args[0] -eq "models") {
    $configPath = "$REPO_DIR\config.yaml"
    $name = ""; $target = ""
    foreach ($line in Get-Content $configPath) {
        if ($line -match '^\s*-\s*model_name:\s*(.+)$') {
            if ($name) { Write-Host ("  {0,-24} -> {1}" -f $name, $(if ($target) { $target } else { "?" })) }
            $name = $Matches[1].Trim(); $target = ""
        } elseif ($line -match '^\s*model:\s*openai/(.+)$' -and -not $target) {
            $target = $Matches[1].Trim()
        }
    }
    if ($name) { Write-Host ("  {0,-24} -> {1}" -f $name, $(if ($target) { $target } else { "?" })) }
    Write-Host ""
    Write-Host "Run with: opclaude --model <name>   (default: claude-deepseek-v4-pro)"
    exit 0
}

# --- set-key --------------------------------------------------------------
if ($args[0] -eq "set-key") {
    if (-not (Test-Path $ENV_FILE)) {
        Write-Error "opclaude is not set up yet. Run install.ps1 first."
        exit 1
    }
    $envVars = @{}
    Get-Content $ENV_FILE | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { $envVars[$Matches[1]] = $Matches[2] }
    }
    $newKey = if ($args.Count -gt 1) { $args[1] } else {
        $ss = Read-Host -AsSecureString "Enter your new opencode API key"
        [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ss))
    }
    if (-not $newKey) { Write-Host "No key entered, nothing changed."; exit 1 }
    $envVars["OPENCODE_API_KEY"] = $newKey
    ($envVars.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "`n" |
        Out-File $ENV_FILE -Encoding utf8 -NoNewline
    Write-Host "Updated OPENCODE_API_KEY in $ENV_FILE."
    $proxyCmd = Get-Command "opclaude-proxy" -ErrorAction SilentlyContinue
    if ($proxyCmd) { & opclaude-proxy restart }
    exit 0
}

# --- enable-ide -----------------------------------------------------------
if ($args[0] -eq "enable-ide") {
    if (-not (Test-Path $ENV_FILE)) { Write-Error "opclaude is not set up yet. Run install.ps1 first."; exit 1 }
    $envVars = @{}
    Get-Content $ENV_FILE | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { $envVars[$Matches[1]] = $Matches[2] }
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $SETTINGS) | Out-Null
    $settings = if (Test-Path $SETTINGS) {
        Get-Content $SETTINGS -Raw | ConvertFrom-Json
    } else { [PSCustomObject]@{} }
    if (-not ($settings | Get-Member "env" -ErrorAction SilentlyContinue)) {
        $settings | Add-Member -NotePropertyName "env" -NotePropertyValue ([PSCustomObject]@{})
    }
    $settings.env | Add-Member -NotePropertyName "ANTHROPIC_BASE_URL" -NotePropertyValue "http://127.0.0.1:4000" -Force
    $settings.env | Add-Member -NotePropertyName "ANTHROPIC_AUTH_TOKEN" -NotePropertyValue $envVars["LITELLM_MASTER_KEY"] -Force
    $settings | ConvertTo-Json -Depth 10 | Out-File $SETTINGS -Encoding utf8
    Write-Host "Added ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN to $SETTINGS."
    Write-Host "Every claude session now routes through opclaude -- CLI, VSCode, and JetBrains alike."
    Write-Host "Run 'opclaude disable-ide' to revert."
    & opclaude-proxy ensure
    exit 0
}

# --- disable-ide ----------------------------------------------------------
if ($args[0] -eq "disable-ide") {
    if (-not (Test-Path $SETTINGS)) { Write-Host "$SETTINGS does not exist, nothing to do."; exit 0 }
    $settings = Get-Content $SETTINGS -Raw | ConvertFrom-Json
    if ($settings | Get-Member "env" -ErrorAction SilentlyContinue) {
        $settings.env.PSObject.Properties.Remove("ANTHROPIC_BASE_URL")
        $settings.env.PSObject.Properties.Remove("ANTHROPIC_AUTH_TOKEN")
        if (($settings.env.PSObject.Properties | Measure-Object).Count -eq 0) {
            $settings.PSObject.Properties.Remove("env")
        }
    }
    $settings | ConvertTo-Json -Depth 10 | Out-File $SETTINGS -Encoding utf8
    Write-Host "Removed ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN from $SETTINGS."
    exit 0
}

# --- guard rails ----------------------------------------------------------
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Error "Claude Code CLI ('claude') not found. Run install.ps1 again, or: npm install -g @anthropic-ai/claude-code"
    exit 1
}
if (-not (Test-Path $ENV_FILE)) {
    Write-Error "opclaude is not set up yet. Run install.ps1 first."
    exit 1
}

# Load env vars into process scope
Get-Content $ENV_FILE | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') {
        [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}

& opclaude-proxy ensure

$DEFAULT_MODEL = "claude-deepseek-v4-pro"
$hasModelFlag  = $args -contains "--model" -or ($args | Where-Object { $_ -match '^--model=' })
$modelArgs     = if (-not $hasModelFlag) { @("--model", $DEFAULT_MODEL) } else { @() }

$env:ANTHROPIC_BASE_URL                   = "http://127.0.0.1:4000"
$env:ANTHROPIC_AUTH_TOKEN                 = $env:LITELLM_MASTER_KEY
$env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = "1"

& claude @modelArgs @args
