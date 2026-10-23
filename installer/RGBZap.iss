; Inno Setup script for RGBZap - turns dist\RGBZap into one Setup.exe.
; Built by build.bat, which passes the version:  ISCC /DAppVersion=1.0.0 RGBZap.iss
; Per-user install (no admin prompt): %LOCALAPPDATA%\Programs\RGBZap.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "RGBZap"
#define AppExe  "RGBZap.exe"
#define RunKey  "Software\Microsoft\Windows\CurrentVersion\Run"

[Setup]
; never change AppId: it's how upgrades and the uninstaller find the existing install
AppId={{7D634CCD-A01B-4C98-AEAE-9BE0CF9ACC1F}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Dilshan Rangaka
AppPublisherURL=https://dilshan-rangaka.vercel.app/
AppSupportURL=https://github.com/TGDRangaka/RGBZap/issues
AppUpdatesURL=https://github.com/TGDRangaka/RGBZap/releases
AppCopyright=Copyright (c) 2026 Dilshan Rangaka
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
SetupIconFile=..\assets\rgbzap.ico
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
Compression=lzma2
SolidCompression=yes
OutputDir=..\dist
OutputBaseFilename=RGBZap-Setup-{#AppVersion}
; close a running RGBZap (it hides in the tray) before replacing its files
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Open RGBZap when &Windows starts"; GroupDescription: "Startup:"

[Files]
Source: "..\dist\RGBZap\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; same value the app's own "Open automatically when Windows starts" toggle writes
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "{#AppName}"; \
  ValueData: """{app}\{#AppExe}"""; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; \
  Flags: nowait postinstall skipifsilent

[Code]
// The app can turn "start with Windows" on later from its System tab, so the
// uninstaller removes that entry whether or not the installer created it.
// Settings in %APPDATA%\RGBZap are kept, so a reinstall picks up where you left off.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RegDeleteValue(HKCU, '{#RunKey}', '{#AppName}');
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run',
                   '{#AppName}');
  end;
end;
