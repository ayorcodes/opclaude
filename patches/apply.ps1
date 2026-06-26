# Reapplies our litellm patches (see ../FIX.md, bug #2) on Windows.
# Requires patch.exe — ships with Git for Windows (winget install Git.Git).
#
# Usage: .\apply.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Find patch.exe — Git for Windows puts it in usr\bin
$patchExe = Get-Command patch -ErrorAction SilentlyContinue
if (-not $patchExe) {
    $candidates = @(
        "C:\Program Files\Git\usr\bin\patch.exe",
        "C:\Program Files (x86)\Git\usr\bin\patch.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $patchExe = $c; break }
    }
}
if (-not $patchExe) {
    Write-Error "patch.exe not found. Install Git for Windows (winget install Git.Git) which includes it."
    exit 1
}
$patchCmd = if ($patchExe -is [System.Management.Automation.ApplicationInfo]) { $patchExe.Source } else { $patchExe }

# Locate litellm site-packages via uv
$sitePkgs = uv tool run --from litellm python -c "import litellm, os; print(os.path.dirname(os.path.dirname(litellm.__file__)))" 2>$null
if (-not $sitePkgs -or -not (Test-Path $sitePkgs)) {
    Write-Error "Could not locate litellm site-packages via uv. Is litellm installed with 'uv tool install litellm'?"
    exit 1
}

Write-Host "Patching litellm install at: $sitePkgs"
Push-Location $sitePkgs

$PATCH_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$status = 0

foreach ($patchFile in Get-ChildItem "$PATCH_DIR\*.patch") {
    $name = $patchFile.Name
    # Dry-run forward
    $dryRun = & $patchCmd -p1 --forward --dry-run -s -i $patchFile.FullName 2>&1
    if ($LASTEXITCODE -eq 0) {
        & $patchCmd -p1 --forward -i $patchFile.FullName
        Write-Host "applied: $name"
    } else {
        # Dry-run reverse (already applied?)
        $dryRunR = & $patchCmd -p1 --forward --dry-run -s -R -i $patchFile.FullName 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "already applied: $name"
        } else {
            Write-Warning "FAILED to apply (litellm internals likely changed upstream -- check ../FIX.md bug #2 and re-derive the patch): $name"
            $status = 1
        }
    }
}

Pop-Location
exit $status
