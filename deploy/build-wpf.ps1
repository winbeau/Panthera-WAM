[CmdletBinding()]
param(
    [ValidateSet("Quick", "Ci", "Installer")]
    [string]$Mode = "Quick",

    [string]$Version = "",

    [string]$PublishPath = "",

    [switch]$SkipUiTests,

    [switch]$SkipInstallerSmoke,

    [switch]$NoSubmoduleUpdate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$scriptRoot = Split-Path -Parent $PSCommandPath
$repoRoot = [IO.Path]::GetFullPath((Join-Path $scriptRoot ".."))
$solution = Join-Path $repoRoot "wpf\Panthera.Terminal.sln"
$appProject = Join-Path $repoRoot "wpf\src\Panthera.Terminal.App\Panthera.Terminal.App.csproj"
$unitTestProject = Join-Path $repoRoot "wpf\tests\Panthera.Terminal.Tests\Panthera.Terminal.Tests.csproj"
$uiTestProject = Join-Path $repoRoot "wpf\tests\Panthera.Terminal.UiTests\Panthera.Terminal.UiTests.csproj"
$installerRoot = Join-Path $repoRoot "wpf\installer"
$installerScript = Join-Path $installerRoot "Panthera-Terminal.iss"
$defaultPublishPath = Join-Path $installerRoot "publish"

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)] [string]$Title,
        [Parameter(Mandatory)] [string]$FilePath,
        [Parameter(Mandatory)] [string[]]$Arguments
    )

    Write-Step $Title
    & $FilePath @Arguments | Out-Host
    $nativeExitCode = Get-Variable LASTEXITCODE -ValueOnly -ErrorAction SilentlyContinue
    if ($null -ne $nativeExitCode -and $nativeExitCode -ne 0) {
        throw "$Title failed with exit code $nativeExitCode"
    }
}

function Resolve-DotNet9 {
    $candidates = [Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($env:PANTHERA_DOTNET_EXE)) {
        $candidates.Add($env:PANTHERA_DOTNET_EXE)
    }
    foreach ($dotnetRoot in @($env:DOTNET_ROOT_X64, $env:DOTNET_ROOT)) {
        if (-not [string]::IsNullOrWhiteSpace($dotnetRoot)) {
            $candidates.Add((Join-Path $dotnetRoot "dotnet.exe"))
        }
    }
    $candidates.Add((Join-Path $HOME ".dotnet\dotnet.exe"))
    $command = Get-Command dotnet -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) {
        $candidates.Add($command.Source)
    }

    $originalDotNetRoot = $env:DOTNET_ROOT
    $originalDotNetRootX64 = $env:DOTNET_ROOT_X64
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        $candidatePath = [IO.Path]::GetFullPath($candidate)
        $candidateRoot = Split-Path -Parent $candidatePath
        try {
            $env:DOTNET_ROOT = $candidateRoot
            $env:DOTNET_ROOT_X64 = $candidateRoot
            $versionOutput = @(& $candidatePath --version 2>$null)
            $candidateExitCode = Get-Variable LASTEXITCODE -ValueOnly -ErrorAction SilentlyContinue
            $detectedVersion = [string]($versionOutput | Where-Object {
                ([string]$_).Trim() -match '^9\.'
            } | Select-Object -First 1)
            if (($null -eq $candidateExitCode -or $candidateExitCode -eq 0) -and -not [string]::IsNullOrWhiteSpace($detectedVersion)) {
                return [pscustomobject]@{
                    Exe = $candidatePath
                    Root = $candidateRoot
                    Version = $detectedVersion.Trim()
                }
            }
        }
        finally {
            $env:DOTNET_ROOT = $originalDotNetRoot
            $env:DOTNET_ROOT_X64 = $originalDotNetRootX64
        }
    }

    throw @"
.NET 9 SDK was not found. Install it into $HOME\.dotnet or set PANTHERA_DOTNET_EXE.
After installation run: .\deploy\setup-dotnet9.ps1
"@
}

function Enable-DotNet9ForProcess($DotNet) {
    $env:DOTNET_ROOT = $DotNet.Root
    $env:DOTNET_ROOT_X64 = $DotNet.Root
    $env:DOTNET_MULTILEVEL_LOOKUP = "0"
    Remove-Item Env:MSBuildSDKsPath -ErrorAction SilentlyContinue

    $pathEntries = @($env:Path -split ';' | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_) -and
        -not $_.TrimEnd('\').Equals($DotNet.Root.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
    })
    $env:Path = (@($DotNet.Root) + $pathEntries) -join ';'
}

function Resolve-ReleaseVersion {
    if (-not [string]::IsNullOrWhiteSpace($Version)) {
        $resolved = $Version
    }
    else {
        [xml]$props = Get-Content -Raw -LiteralPath (Join-Path $repoRoot "wpf\Directory.Build.props")
        $resolved = [string]($props.Project.PropertyGroup.Version | Select-Object -First 1)
    }
    if ($resolved -notmatch '^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$') {
        throw "Invalid release version: $resolved"
    }
    return $resolved
}

function Resolve-OutputPath([string]$RequestedPath, [string]$FallbackPath) {
    $path = if ([string]::IsNullOrWhiteSpace($RequestedPath)) { $FallbackPath } else { $RequestedPath }
    if (-not [IO.Path]::IsPathRooted($path)) {
        $path = Join-Path $repoRoot $path
    }
    return [IO.Path]::GetFullPath($path)
}

function Reset-SafeDirectory([string]$Path, [string]$Purpose) {
    $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $rootPath = [IO.Path]::GetPathRoot($fullPath).TrimEnd('\')
    $userProfile = [IO.Path]::GetFullPath($HOME).TrimEnd('\')
    if ($fullPath -eq $rootPath -or $fullPath -eq $userProfile -or $fullPath -eq $repoRoot.TrimEnd('\')) {
        throw "Refusing to reset unsafe $Purpose directory: $fullPath"
    }
    Write-Host "$Purpose directory: $fullPath"
    if (Test-Path -LiteralPath $fullPath) {
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
    New-Item -ItemType Directory -Path $fullPath -Force | Out-Null
    return $fullPath
}

function Restore-Build-And-Test([bool]$RunUiTests) {
    Invoke-NativeCommand "Restore WPF solution" $dotnet.Exe @("restore", $solution)
    Invoke-NativeCommand "Build WPF Release" $dotnet.Exe @(
        "build", $solution, "--configuration", "Release", "--no-restore"
    )
    Invoke-NativeCommand "Run WPF unit tests" $dotnet.Exe @(
        "test", $unitTestProject, "--configuration", "Release", "--no-build"
    )
    if ($RunUiTests) {
        $env:PANTHERA_RUN_UI_TESTS = "1"
        if ([string]::IsNullOrWhiteSpace($env:PANTHERA_UI_ARTIFACTS)) {
            $env:PANTHERA_UI_ARTIFACTS = Join-Path $env:TEMP "Panthera-WAM\ui-artifacts"
        }
        Invoke-NativeCommand "Run WPF UI tests" $dotnet.Exe @(
            "test", $uiTestProject, "--configuration", "Release", "--no-build"
        )
    }
}

function Publish-Application([string]$Destination, [string]$ReleaseVersion) {
    $publishRoot = Reset-SafeDirectory $Destination "WPF publish"
    $arguments = @(
        "publish", $appProject,
        "--configuration", "Release",
        "--runtime", "win-x64",
        "--self-contained", "true",
        "--output", $publishRoot
    )
    if (-not [string]::IsNullOrWhiteSpace($ReleaseVersion)) {
        $arguments += "-p:Version=$ReleaseVersion"
    }
    Invoke-NativeCommand "Publish self-contained win-x64 application" $dotnet.Exe $arguments

    $appExe = Join-Path $publishRoot "Panthera.Terminal.App.exe"
    if (-not (Test-Path -LiteralPath $appExe -PathType Leaf)) {
        throw "Published executable was not found: $appExe"
    }
    return [pscustomobject]@{ Root = $publishRoot; Exe = $appExe }
}

function Ensure-ChineseInstallerMessages {
    $path = Join-Path $installerRoot "ChineseSimplified.isl"
    $expected = "6753be2c5e2740d859900fd902824db2ec568da5c5b52486524c9762d778b0b0"
    if (Test-Path -LiteralPath $path) {
        $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -eq $expected) {
            return $path
        }
        Remove-Item -LiteralPath $path -Force
    }

    Write-Step "Download pinned Simplified Chinese Inno Setup messages"
    $url = "https://raw.githubusercontent.com/jrsoftware/issrc/683ee7eabfbce807f901c5da83fc5ff1a3ecb693/Files/Languages/ChineseSimplified.isl"
    Invoke-WebRequest $url -OutFile $path
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "ChineseSimplified.isl checksum mismatch: $actual"
    }
    return $path
}

function Resolve-InnoCompiler {
    $candidates = @(
        $env:PANTHERA_ISCC,
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $command = Get-Command ISCC.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) {
        $candidates += $command.Source
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    throw "Inno Setup 6 was not found. Install it with: winget install JRSoftware.InnoSetup"
}

function Build-Installer([string]$ReleaseVersion) {
    Ensure-ChineseInstallerMessages | Out-Null
    $iscc = Resolve-InnoCompiler
    $outputRoot = Reset-SafeDirectory (Join-Path $installerRoot "output") "Installer output"
    $stableVersion = ($ReleaseVersion -split '-', 2)[0]
    $env:PANTHERA_INSTALLER_VERSION = $ReleaseVersion
    $env:PANTHERA_INSTALLER_FILE_VERSION = "$stableVersion.0"
    Invoke-NativeCommand "Compile Inno Setup installer" $iscc @($installerScript)

    $installers = @(Get-ChildItem -LiteralPath $outputRoot -Filter "*-setup.exe" -File)
    if ($installers.Count -ne 1) {
        throw "Expected one installer, found $($installers.Count)"
    }
    $installer = $installers[0]
    $checksumPath = Join-Path $outputRoot "SHA256SUMS.txt"
    $checksum = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$checksum  $($installer.Name)" | Set-Content -LiteralPath $checksumPath -Encoding ascii
    return [pscustomobject]@{ Installer = $installer.FullName; Checksum = $checksumPath }
}

function Test-InstallerPackage($Package) {
    $installDir = Join-Path $env:TEMP "Panthera-Terminal-Installed-$PID"
    $installLog = Join-Path $env:TEMP "panthera-installer-$PID.log"
    Reset-SafeDirectory $installDir "Installer smoke-test" | Out-Null

    Write-Step "Smoke-test installer, application screenshot, and uninstaller"
    $install = Start-Process $Package.Installer -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/DIR=$installDir", "/LOG=$installLog"
    ) -WindowStyle Hidden -PassThru -Wait
    if ($install.ExitCode -ne 0) {
        Get-Content -LiteralPath $installLog -ErrorAction SilentlyContinue
        throw "Installer exited with code $($install.ExitCode)"
    }

    $app = Join-Path $installDir "Panthera.Terminal.App.exe"
    if (-not (Test-Path -LiteralPath $app -PathType Leaf)) {
        throw "Installed application was not found: $app"
    }
    $screenshot = Join-Path $env:TEMP "panthera-installed-highcontrast-$PID.png"
    $env:PANTHERA_UI_TEST = "1"
    $env:PANTHERA_UI_ACCEPTANCE = "1"
    $env:PANTHERA_SCREENSHOT_THEME = "HighContrast"
    $env:PANTHERA_SCREENSHOT_PATH = $screenshot
    $application = Start-Process $app -WindowStyle Hidden -PassThru
    if (-not $application.WaitForExit(30000)) {
        $application.Kill($true)
        throw "Installed application did not finish its screenshot smoke test"
    }
    if ($application.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $screenshot)) {
        throw "Installed application smoke test failed with code $($application.ExitCode)"
    }

    $uninstaller = Join-Path $installDir "unins000.exe"
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        throw "Uninstaller was not found: $uninstaller"
    }
    $uninstall = Start-Process $uninstaller -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
    ) -WindowStyle Hidden -PassThru -Wait
    if ($uninstall.ExitCode -ne 0) {
        throw "Uninstaller exited with code $($uninstall.ExitCode)"
    }
    if (Test-Path -LiteralPath $app) {
        throw "Application files remain after uninstall"
    }
    Remove-Item -LiteralPath $installDir -Force -ErrorAction SilentlyContinue
}

$dotnet = Resolve-DotNet9
Enable-DotNet9ForProcess $dotnet
Write-Host "Using .NET SDK $($dotnet.Version): $($dotnet.Exe)" -ForegroundColor Green

if (-not $NoSubmoduleUpdate) {
    Invoke-NativeCommand "Initialize WPF CAD visual submodule" "git" @(
        "-C", $repoRoot, "submodule", "update", "--init", "--recursive", "--", "vendor/Panthera-HT-TriView"
    )
}

$releaseVersion = Resolve-ReleaseVersion
$requestedPublishPath = Resolve-OutputPath $PublishPath $defaultPublishPath

switch ($Mode) {
    "Quick" {
        $published = Publish-Application $requestedPublishPath $releaseVersion
    }
    "Ci" {
        Restore-Build-And-Test (-not $SkipUiTests)
        $published = Publish-Application $requestedPublishPath $releaseVersion
    }
    "Installer" {
        Restore-Build-And-Test $false
        $published = Publish-Application $defaultPublishPath $releaseVersion
        $package = Build-Installer $releaseVersion
        if (-not $SkipInstallerSmoke) {
            Test-InstallerPackage $package
        }
    }
}

Write-Host "`nWPF executable ready:" -ForegroundColor Green
Write-Host "  $($published.Exe)"

if ($Mode -eq "Installer") {
    Write-Host "Installer ready:" -ForegroundColor Green
    Write-Host "  $($package.Installer)"
    Write-Host "  $($package.Checksum)"
}

if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_OUTPUT)) {
    "publish_path=$($published.Root)" | Add-Content -LiteralPath $env:GITHUB_OUTPUT -Encoding utf8
    "executable=$($published.Exe)" | Add-Content -LiteralPath $env:GITHUB_OUTPUT -Encoding utf8
    if ($Mode -eq "Installer") {
        "installer=$($package.Installer)" | Add-Content -LiteralPath $env:GITHUB_OUTPUT -Encoding utf8
        "checksum_file=$($package.Checksum)" | Add-Content -LiteralPath $env:GITHUB_OUTPUT -Encoding utf8
    }
}
