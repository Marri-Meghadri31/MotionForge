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
uv run pyinstaller --clean --noconfirm prompt-animator.spec

Write-Host "Built: $ProjectRoot\dist\prompt-animator\prompt-animator.exe"
