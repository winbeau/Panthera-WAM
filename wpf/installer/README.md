# Windows installer

`Panthera-Terminal.iss` packages the self-contained `win-x64` WPF publish output as a single
per-user Inno Setup installer. The installer writes to
`%LOCALAPPDATA%\Programs\Panthera-Terminal`, creates a Start menu entry, offers an optional desktop
shortcut, and registers a normal Windows uninstaller.

The GitHub workflow `.github/workflows/windows-installer.yml` runs automatically for future `v*`
tags and can also be dispatched manually. A manual dispatch can attach the generated installer and
`SHA256SUMS.txt` to an existing GitHub Release by setting `release_tag`. Before upload, the workflow
silently installs the package, launches the installed application for a HighContrast screenshot,
and silently uninstalls it again.

Local Windows build outline:

```powershell
dotnet publish ..\src\Panthera.Terminal.App\Panthera.Terminal.App.csproj `
  --configuration Release --runtime win-x64 --self-contained true --output publish
$env:PANTHERA_INSTALLER_VERSION = "1.0.0"
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" .\Panthera-Terminal.iss
```
