$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DistributionRoot = Join-Path $ProjectRoot "dist"
$OutputDirectory = Join-Path $DistributionRoot "prompt-animator"
$StagingRoot = Join-Path $ProjectRoot ".tmp-pyinstaller-dist-$PID"
Set-Location -LiteralPath $ProjectRoot

# Keep builds independent of the permissions and state of uv's global cache.
if ([string]::IsNullOrWhiteSpace($env:UV_CACHE_DIR)) {
    $env:UV_CACHE_DIR = Join-Path $ProjectRoot ".uv-cache"
}

uv sync
if ($LASTEXITCODE -ne 0) {
    throw "uv sync failed with exit code $LASTEXITCODE"
}

try {
    uv run pyinstaller --clean --noconfirm --distpath $StagingRoot prompt-animator.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    $StagedOutputDirectory = Join-Path $StagingRoot "prompt-animator"
    if (-not (Test-Path -LiteralPath $StagedOutputDirectory)) {
        throw "PyInstaller completed without creating '$StagedOutputDirectory'"
    }

    New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
    try {
        Get-ChildItem -Force -LiteralPath $OutputDirectory | Remove-Item -Recurse -Force
    }
    catch {
        throw "Cannot replace '$OutputDirectory'. Close Velo and prompt-animator.exe, then retry. $($_.Exception.Message)"
    }

    Get-ChildItem -Force -LiteralPath $StagedOutputDirectory | ForEach-Object {
        Copy-Item -Recurse -Force -LiteralPath $_.FullName -Destination $OutputDirectory
    }
}
finally {
    if (Test-Path -LiteralPath $StagingRoot) {
        Remove-Item -Recurse -Force -LiteralPath $StagingRoot -ErrorAction SilentlyContinue
    }
}

Write-Host "Built: $OutputDirectory\prompt-animator.exe"
