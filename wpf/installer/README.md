# Windows installer

`Panthera-Terminal.iss` packages the self-contained `win-x64` WPF publish output as a single
per-user Inno Setup installer. The installer writes to
`%LOCALAPPDATA%\Programs\Panthera-Terminal`, creates a Start menu entry, offers an optional desktop
shortcut, and registers a normal Windows uninstaller.
The setup executable, installed application, Start menu shortcut, desktop shortcut, and uninstall
entry all use the shared `Assets/Brand/Panthera.Terminal.ico` multi-resolution icon.

The GitHub workflow `.github/workflows/windows-installer.yml` runs automatically for future `v*`
tags and can also be dispatched manually. A manual dispatch can attach the generated installer and
`SHA256SUMS.txt` to an existing GitHub Release by setting `release_tag`. The workflow and local
builds both call `deploy/build-wpf.ps1`, so restore, tests, self-contained publish, Inno Setup
packaging, checksum generation, screenshot smoke test, and uninstall verification cannot drift.

Local Windows build outline:

```powershell
.\deploy\setup-dotnet9.ps1
panthera-wpf -Mode Installer
```

The installer is written to `wpf/installer/output/`; the unpacked runnable application is kept in
`wpf/installer/publish/`.
