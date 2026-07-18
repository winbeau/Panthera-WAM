@echo off
setlocal
set "DOTNET_ROOT=%USERPROFILE%\.dotnet"
set "MSBuildSDKsPath="
set "PATH=%DOTNET_ROOT%;%PATH%"
set "PANTHERA_RUN_UI_TESTS=1"
set "PANTHERA_UI_ARTIFACTS=%USERPROFILE%\Desktop\Panthera-Design\ui-artifacts"
cd /d "%USERPROFILE%\source\repos\Panthera-WAM"
if not exist "%PANTHERA_UI_ARTIFACTS%" mkdir "%PANTHERA_UI_ARTIFACTS%"
"%DOTNET_ROOT%\dotnet.exe" test wpf\tests\Panthera.Terminal.UiTests\Panthera.Terminal.UiTests.csproj --configuration Release --no-restore --logger "trx;LogFileName=%PANTHERA_UI_ARTIFACTS%\ui-test.trx" > "%PANTHERA_UI_ARTIFACTS%\ui-test.log" 2>&1
exit /b %ERRORLEVEL%
