[CmdletBinding()]
param(
    [string]$MagickCommand = "magick"
)

$ErrorActionPreference = "Stop"

$wpfRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$brandRoot = Join-Path $wpfRoot "src\Panthera.Terminal.App\Assets\Brand"
$sourceSvg = Join-Path $brandRoot "panthera-terminal-app-icon.svg"
$smallSourceSvg = Join-Path $brandRoot "panthera-terminal-app-icon-small.svg"
$markSvg = Join-Path $brandRoot "panthera-terminal-mark.svg"
$whiteMarkSvg = Join-Path $brandRoot "panthera-terminal-mark-white.svg"
$pngRoot = Join-Path $brandRoot "png"
$iconPath = Join-Path $brandRoot "Panthera.Terminal.ico"
$titleBarPath = Join-Path $brandRoot "panthera-terminal-titlebar.png"
$sizes = @(16, 20, 24, 32, 40, 48, 64, 128, 256, 512)

if (-not (Get-Command $MagickCommand -ErrorAction SilentlyContinue)) {
    throw "ImageMagick command '$MagickCommand' was not found. Install ImageMagick 7 first."
}

if (
    -not (Test-Path -LiteralPath $sourceSvg) -or
    -not (Test-Path -LiteralPath $smallSourceSvg) -or
    -not (Test-Path -LiteralPath $markSvg) -or
    -not (Test-Path -LiteralPath $whiteMarkSvg)
) {
    throw "One or more SVG sources are missing from $brandRoot"
}

New-Item -ItemType Directory -Force -Path $pngRoot | Out-Null

$pngFiles = foreach ($size in $sizes) {
    $output = Join-Path $pngRoot "panthera-terminal-$size.png"
    $inputSvg = if ($size -le 24) { $smallSourceSvg } else { $sourceSvg }
    & $MagickCommand -background none $inputSvg -filter Lanczos -resize "${size}x${size}" -strip "PNG32:$output"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to render $size px PNG."
    }
    $output
}

$icoSources = $pngFiles | Where-Object {
    $name = [System.IO.Path]::GetFileNameWithoutExtension($_)
    [int]($name -replace '^panthera-terminal-', '') -le 256
}
& $MagickCommand @icoSources -strip $iconPath
if ($LASTEXITCODE -ne 0) {
    throw "Failed to build Windows ICO."
}

Copy-Item -LiteralPath (Join-Path $pngRoot "panthera-terminal-64.png") -Destination $titleBarPath -Force

& $MagickCommand -background none $markSvg -filter Lanczos -resize "512x512" -strip "PNG32:$(Join-Path $pngRoot 'panthera-terminal-mark-512.png')"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to render the transparent brand mark."
}

& $MagickCommand -background none $whiteMarkSvg -filter Lanczos -resize "512x512" -strip "PNG32:$(Join-Path $pngRoot 'panthera-terminal-mark-white-512.png')"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to render the reversed brand mark."
}

Write-Output "Generated brand assets in $brandRoot"
& $MagickCommand identify $iconPath
