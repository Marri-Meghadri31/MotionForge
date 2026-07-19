param(
    [string]$FfmpegPath = $env:MOTIONFORGE_FFMPEG
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if ($FfmpegPath) {
    $ResolvedFfmpeg = (Resolve-Path -LiteralPath $FfmpegPath).Path
    $env:MOTIONFORGE_FFMPEG = $ResolvedFfmpeg
} else {
    $FfmpegCommand = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($FfmpegCommand) {
        $env:MOTIONFORGE_FFMPEG = $FfmpegCommand.Source
    }
}

uv sync
if ($LASTEXITCODE -ne 0) {
    throw "uv sync failed with exit code $LASTEXITCODE"
}
uv run pyinstaller --clean --noconfirm prompt-animator.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller onedir build failed with exit code $LASTEXITCODE"
}
uv run pyinstaller --clean --noconfirm --workpath build/prompt-animator-onefile prompt-animator-onefile.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller legacy one-file build failed with exit code $LASTEXITCODE"
}

Write-Host "Built: $ProjectRoot\dist\prompt-animator\prompt-animator.exe"
Write-Host "Built legacy compatibility executable: $ProjectRoot\dist\prompt-animator.exe"
