# Manages the local litellm proxy on Windows.
# Usage: opclaude-proxy start|stop|restart|status|ensure

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$REPO_DIR   = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$STATE_DIR  = "$env:USERPROFILE\.config\opclaude"
$ENV_FILE   = "$STATE_DIR\.env"
$PID_FILE   = "$STATE_DIR\proxy.pid"
$LOG_FILE   = "$STATE_DIR\proxy.log"
$PORT       = 4000
$HOST_ADDR  = "127.0.0.1"

New-Item -ItemType Directory -Force -Path $STATE_DIR | Out-Null

if (-not (Test-Path $ENV_FILE)) {
    Write-Error "opclaude is not set up yet. Run install.ps1 from $REPO_DIR first."
    exit 1
}

# Load env vars
Get-Content $ENV_FILE | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') {
        [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
}

function Test-ProxyRunning {
    if (-not (Test-Path $PID_FILE)) { return $false }
    $pid = (Get-Content $PID_FILE -ErrorAction SilentlyContinue).Trim()
    if (-not $pid) { return $false }
    $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    try {
        $resp = Invoke-WebRequest -Uri "http://${HOST_ADDR}:${PORT}/health/liveliness" `
            -TimeoutSec 2 -ErrorAction Stop -UseBasicParsing
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Start-Proxy {
    if (Test-ProxyRunning) {
        $pid = (Get-Content $PID_FILE).Trim()
        Write-Host "opclaude proxy already running (pid $pid)."
        return
    }
    Remove-Item $PID_FILE -ErrorAction SilentlyContinue
    Write-Host "Starting opclaude proxy on http://${HOST_ADDR}:${PORT} ..."

    $proc = Start-Process -FilePath "litellm" `
        -ArgumentList "--config `"$REPO_DIR\config.yaml`" --host $HOST_ADDR --port $PORT" `
        -WorkingDirectory $REPO_DIR `
        -NoNewWindow `
        -RedirectStandardOutput $LOG_FILE `
        -RedirectStandardError "$STATE_DIR\proxy.err.log" `
        -PassThru
    $proc.Id | Out-File $PID_FILE -Encoding ascii -NoNewline

    $waited = 0
    while ($waited -lt 30) {
        Start-Sleep -Seconds 1
        if (Test-ProxyRunning) {
            Write-Host "opclaude proxy is up (pid $($proc.Id))."
            return
        }
        $waited++
    }
    Write-Error "opclaude proxy did not come up within 30s. Check $LOG_FILE for details."
    exit 1
}

function Stop-Proxy {
    if (Test-Path $PID_FILE) {
        $pid = (Get-Content $PID_FILE -ErrorAction SilentlyContinue).Trim()
        if ($pid) {
            $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
            if ($proc) {
                Stop-Process -Id $pid -Force
                Write-Host "Stopped opclaude proxy (pid $pid)."
            }
        }
        Remove-Item $PID_FILE -Force
    } else {
        Write-Host "opclaude proxy is not running."
    }
}

function Get-ProxyStatus {
    if (Test-ProxyRunning) {
        $pid = (Get-Content $PID_FILE).Trim()
        Write-Host "opclaude proxy is running (pid $pid) on http://${HOST_ADDR}:${PORT}"
    } else {
        Write-Host "opclaude proxy is not running."
    }
}

switch ($args[0]) {
    "start"   { Start-Proxy }
    "stop"    { Stop-Proxy }
    "restart" { Stop-Proxy; Start-Sleep -Seconds 1; Start-Proxy }
    "status"  { Get-ProxyStatus }
    "ensure"  { if (-not (Test-ProxyRunning)) { Start-Proxy } }
    default {
        Write-Error "Usage: opclaude-proxy start|stop|restart|status|ensure"
        exit 1
    }
}
