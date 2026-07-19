#define MyAppName "Panthera Terminal"
#define MyAppPublisher "winbeau"
#define MyAppURL "https://github.com/winbeau/Panthera-WAM"
#define MyAppExeName "Panthera.Terminal.App.exe"
#define MyAppVersion GetEnv("PANTHERA_INSTALLER_VERSION")
#define MyAppFileVersion GetEnv("PANTHERA_INSTALLER_FILE_VERSION")

#if MyAppVersion == ""
  #define MyAppVersion "1.0.0"
#endif
#if MyAppFileVersion == ""
  #define MyAppFileVersion "1.0.0.0"
#endif

[Setup]
AppId={{B4896A64-7585-4FF2-B641-04B3D992EE6D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#MyAppFileVersion}
DefaultDirName={localappdata}\Programs\Panthera-Terminal
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=output
OutputBaseFilename=Panthera-Terminal-v{#MyAppVersion}-win-x64-setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern dynamic
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "publish\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
