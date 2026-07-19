[CmdletBinding()]
param(
    [string]$DotNetRoot = (Join-Path $HOME ".dotnet"),

    [string]$RepoRoot = ([IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $PSCommandPath) ".."))),

    [ValidateSet("PowerShell", "WindowsPowerShell", "All")]
    [string]$Shell = "All"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$dotnetRootFull = [IO.Path]::GetFullPath($DotNetRoot)
$dotnetExe = Join-Path $dotnetRootFull "dotnet.exe"
if (-not (Test-Path -LiteralPath $dotnetExe -PathType Leaf)) {
    throw ".NET executable was not found: $dotnetExe"
}
$versionOutput = & $dotnetExe --version
$dotnetExitCode = Get-Variable LASTEXITCODE -ValueOnly -ErrorAction SilentlyContinue
$sdkVersion = ([string]($versionOutput | Select-Object -First 1)).Trim()
if (($null -ne $dotnetExitCode -and $dotnetExitCode -ne 0) -or $sdkVersion -notmatch '^9\.') {
    throw "Expected a .NET 9 SDK at $dotnetExe, detected: $sdkVersion"
}

$repoRootFull = [IO.Path]::GetFullPath($RepoRoot)
$buildScript = Join-Path $repoRootFull "deploy\build-wpf.ps1"
if (-not (Test-Path -LiteralPath $buildScript -PathType Leaf)) {
    throw "WPF build script was not found: $buildScript"
}

$markerStart = "# >>> Panthera-WAM .NET 9 toolchain >>>"
$markerEnd = "# <<< Panthera-WAM .NET 9 toolchain <<<"
$escapedBuildScript = $buildScript.Replace("'", "''")
$profileBlock = @'
# >>> Panthera-WAM .NET 9 toolchain >>>
$pantheraDotNetRoot = Join-Path $HOME ".dotnet"
$pantheraDotNetExe = Join-Path $pantheraDotNetRoot "dotnet.exe"
if (Test-Path -LiteralPath $pantheraDotNetExe) {
    $env:DOTNET_ROOT = $pantheraDotNetRoot
    $env:DOTNET_ROOT_X64 = $pantheraDotNetRoot
    $env:DOTNET_MULTILEVEL_LOOKUP = "0"
    Remove-Item Env:MSBuildSDKsPath -ErrorAction SilentlyContinue
    $pantheraPathEntries = @($env:Path -split ';' | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_) -and
        -not $_.TrimEnd('\').Equals($pantheraDotNetRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
    })
    $env:Path = (@($pantheraDotNetRoot) + $pantheraPathEntries) -join ';'
    Set-Alias -Name dotnet -Value $pantheraDotNetExe -Scope Global
}

$pantheraWpfBuildScript = '__BUILD_SCRIPT__'
function global:panthera-wpf {
    & $pantheraWpfBuildScript @args
}
# <<< Panthera-WAM .NET 9 toolchain <<<
'@.Replace("__BUILD_SCRIPT__", $escapedBuildScript).Trim()

function Update-Profile([string]$ProfilePath) {
    $directory = Split-Path -Parent $ProfilePath
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $existing = if (Test-Path -LiteralPath $ProfilePath) {
        Get-Content -Raw -LiteralPath $ProfilePath
    }
    else {
        ""
    }

    $pattern = "(?s)" + [regex]::Escape($markerStart) + ".*?" + [regex]::Escape($markerEnd)
    if ($existing -match $pattern) {
        $updated = [regex]::Replace(
            $existing,
            $pattern,
            [Text.RegularExpressions.MatchEvaluator]{ param($match) $profileBlock })
    }
    elseif ([string]::IsNullOrWhiteSpace($existing)) {
        $updated = "$profileBlock`r`n"
    }
    else {
        $updated = $existing.TrimEnd() + "`r`n`r`n$profileBlock`r`n"
    }
    Set-Content -LiteralPath $ProfilePath -Value $updated -Encoding utf8
    Write-Host "Configured: $ProfilePath" -ForegroundColor Green
}

$documents = [Environment]::GetFolderPath([Environment+SpecialFolder]::MyDocuments)
$profilePaths = switch ($Shell) {
    "PowerShell" { Join-Path $documents "PowerShell\Microsoft.PowerShell_profile.ps1" }
    "WindowsPowerShell" { Join-Path $documents "WindowsPowerShell\Microsoft.PowerShell_profile.ps1" }
    "All" {
        Join-Path $documents "PowerShell\Microsoft.PowerShell_profile.ps1"
        Join-Path $documents "WindowsPowerShell\Microsoft.PowerShell_profile.ps1"
    }
}

foreach ($profilePath in $profilePaths) {
    Update-Profile $profilePath
}

$env:DOTNET_ROOT = $dotnetRootFull
$env:DOTNET_ROOT_X64 = $dotnetRootFull
$env:DOTNET_MULTILEVEL_LOOKUP = "0"
Remove-Item Env:MSBuildSDKsPath -ErrorAction SilentlyContinue
$env:Path = "$dotnetRootFull;$env:Path"
Set-Alias -Name dotnet -Value $dotnetExe -Scope Global

Write-Host "`n.NET $sdkVersion is now selected for this process and future PowerShell sessions."
Write-Host "Quick WPF publish: panthera-wpf"
Write-Host "CI-equivalent build: panthera-wpf -Mode Ci"
Write-Host "Installer build: panthera-wpf -Mode Installer"
