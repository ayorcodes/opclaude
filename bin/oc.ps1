# oc (Windows): routes a Claude Code task to a cheap opencode model or real
# Claude based on task complexity. See ../README.md.
#
# Usage:
#   oc "<task>"           classify, route, launch
#   oc classify "<task>"  print the routing decision only
#   oc --escalate         resume last oc session on real Claude (Pro)
#   oc --cheap            continue most recent session on the cheap proxy

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$REPO_DIR         = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$STATE_DIR        = "$env:USERPROFILE\.config\opclaude"
$ENV_FILE         = "$STATE_DIR\.env"
$ROUTER_CONFIG    = "$STATE_DIR\router.yaml"
$LOG_FILE         = "$STATE_DIR\router.log"
$LAST_SESSION     = "$STATE_DIR\last_session"

if (-not (Test-Path $ENV_FILE)) {
    Write-Error "opclaude is not set up yet. Run install.ps1 first."
    exit 1
}

# Load env vars
Get-Content $ENV_FILE | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') {
        [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}

function Invoke-Classify([string]$task) {
    uv run python "$REPO_DIR\bin\oc-classify" --task $task --router-config $ROUTER_CONFIG
}

# --- classify sub-command -------------------------------------------------
if ($args[0] -eq "classify") {
    $task = ($args[1..($args.Count-1)]) -join " "
    if (-not $task) { Write-Error "Usage: oc classify `"<task description>`""; exit 1 }
    uv run python "$REPO_DIR\bin\oc-classify" --task $task --router-config $ROUTER_CONFIG
    exit $LASTEXITCODE
}

# --- --escalate -----------------------------------------------------------
if ($args[0] -eq "--escalate") {
    if (-not (Test-Path $LAST_SESSION)) {
        Write-Error "No previous oc session found. Run 'oc `"<task>`"' first."
        exit 1
    }
    $sessionVars = @{}
    Get-Content $LAST_SESSION | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') { $sessionVars[$Matches[1]] = $Matches[2] }
    }
    $sessionId = $sessionVars["SESSION_ID"]
    $extra = ($args[1..($args.Count-1)]) -join " "
    Write-Host "oc: escalating session $sessionId to real Claude (Pro, no proxy)." -ForegroundColor Cyan
    if ($extra) {
        & claude --resume $sessionId --fork-session $extra
    } else {
        & claude --resume $sessionId --fork-session
    }
    exit $LASTEXITCODE
}

# --- --cheap / --downgrade ------------------------------------------------
if ($args[0] -eq "--cheap" -or $args[0] -eq "--downgrade") {
    & opclaude-proxy ensure
    $extra = ($args[1..($args.Count-1)]) -join " "
    $env:ANTHROPIC_BASE_URL                    = "http://127.0.0.1:4000"
    $env:ANTHROPIC_AUTH_TOKEN                  = $env:LITELLM_MASTER_KEY
    $env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = "1"
    Write-Host "oc: continuing most recent session on the cheap proxy (auto-routed)." -ForegroundColor Cyan
    if ($extra) {
        & claude --continue --fork-session --model claude-auto $extra
    } else {
        & claude --continue --fork-session --model claude-auto
    }
    exit $LASTEXITCODE
}

# --- main routing ---------------------------------------------------------
$TASK = $args -join " "
if (-not $TASK) {
    Write-Host "Usage: oc `"<task description>`""
    Write-Host "       oc classify `"<task description>`""
    Write-Host "       oc --escalate [extra context]"
    Write-Host "       oc --cheap [extra context]"
    exit 1
}

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Error "Claude Code CLI ('claude') not found."
    exit 1
}

$decisionJson = Invoke-Classify $TASK
$decision     = $decisionJson | ConvertFrom-Json
$TIER         = $decision.tier
$MODEL        = $decision.model

if ($TIER -eq "critical") {
    Write-Host "oc: routing to real Claude --model $MODEL (critical reasoning task)." -ForegroundColor Yellow
    & claude --model $MODEL @args
    exit $LASTEXITCODE
}

& opclaude-proxy ensure

$env:ANTHROPIC_BASE_URL                    = "http://127.0.0.1:4000"
$env:ANTHROPIC_AUTH_TOKEN                  = $env:LITELLM_MASTER_KEY
$env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = "1"

# Generate session ID (uuidgen equivalent)
$SESSION_ID = [System.Guid]::NewGuid().ToString().ToUpper()

@"
SESSION_ID=$SESSION_ID
TASK=$TASK
MODEL=$MODEL
TIER=$TIER
CWD=$(Get-Location)
TIMESTAMP=$(Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
"@ | Out-File $LAST_SESSION -Encoding utf8

Write-Host "oc: auto-routing this session via opclaude proxy (first turn: $TIER -> $MODEL)." -ForegroundColor Cyan
& claude --model claude-auto --session-id $SESSION_ID @args
