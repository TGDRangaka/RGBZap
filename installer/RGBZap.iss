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
; tells Windows the PATH changed (the optional "rgbzap" command task) so new terminals see it
ChangesEnvironment=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Open RGBZap when &Windows starts"; GroupDescription: "Startup:"
Name: "addpath"; Description: "Add the &rgbzap command to PATH (control RGBZap from a terminal)"; GroupDescription: "Command line:"; Flags: unchecked

[Files]
Source: "..\dist\RGBZap\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; same value the app's own "Open automatically when Windows starts" toggle writes
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "{#AppName}"; \
  ValueData: """{app}\{#AppExe}"""; Tasks: startup
; the per-user PATH, so `rgbzap color red` works in any new terminal (no admin needed)
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
  ValueData: "{olddata};{app}\cli"; Tasks: addpath; Check: NeedsAddPath(ExpandConstant('{app}\cli'))

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; \
  Flags: nowait postinstall skipifsilent

[Code]
// The app can turn "start with Windows" on later from its System tab, so the
// uninstaller removes that entry whether or not the installer created it.
// Settings in %APPDATA%\RGBZap are kept, so a reinstall picks up where you left off.
// True unless the folder is already on the user's PATH (so re-installing never duplicates it).
function NeedsAddPath(Dir: string): Boolean;
var
  Path: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Path) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';') = 0;
end;

// Takes the install folder back off the user's PATH when uninstalling.
procedure RemoveFromPath(Dir: string);
var
  Path, Padded: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Path) then
    exit;
  Padded := ';' + Path + ';';
  P := Pos(';' + Uppercase(Dir) + ';', Uppercase(Padded));
  if P = 0 then
    exit;
  Delete(Padded, P, Length(Dir) + 1);                  // removes ";Dir"
  Path := Copy(Padded, 2, Length(Padded) - 2);         // drop the padding semicolons
  RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Path);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RemoveFromPath(ExpandConstant('{app}\cli'));
    RegDeleteValue(HKCU, '{#RunKey}', '{#AppName}');
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run',
                   '{#AppName}');
  end;
end;
