param(
    [string]$Bind = "127.0.0.1:50052",
    [string]$Serial = "",
    [int]$Width = 640,
    [int]$Height = 480,
    [int]$Fps = 30
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

Push-Location $RepoRoot
try {
    uv run --package panthera-camera --extra realsense camerad `
        --mode auto `
        --bind $Bind `
        --serial $Serial `
        --width $Width `
        --height $Height `
        --fps $Fps
}
finally {
    Pop-Location
}
