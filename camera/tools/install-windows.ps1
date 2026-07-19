param()

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

Push-Location $RepoRoot
try {
    uv python install 3.11
    uv sync --package panthera-camera --extra realsense
    Write-Host "camerad installed in $RepoRoot\.venv" -ForegroundColor Green
}
finally {
    Pop-Location
}
