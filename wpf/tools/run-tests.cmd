@echo off
setlocal
if not defined DOTNET_ROOT set "DOTNET_ROOT=%USERPROFILE%\.dotnet"
set "MSBuildSDKsPath="
set "PATH=%DOTNET_ROOT%;%PATH%"
for %%I in ("%~dp0..\..") do set "REPO_ROOT=%%~fI"
cd /d "%REPO_ROOT%"

"%DOTNET_ROOT%\dotnet.exe" restore wpf\Panthera.Terminal.sln || exit /b 1
"%DOTNET_ROOT%\dotnet.exe" build wpf\Panthera.Terminal.sln --configuration Release --no-restore || exit /b 1
"%DOTNET_ROOT%\dotnet.exe" test wpf\tests\Panthera.Terminal.Tests\Panthera.Terminal.Tests.csproj --configuration Release --no-build || exit /b 1
call wpf\tools\run-ui-tests.cmd || exit /b 1
