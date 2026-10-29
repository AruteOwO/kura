; Kura's installer (Inno Setup 6). release.py builds it:
;   ISCC /DAppVersion=1.1.0 /DSourceDir=<repo> /DManifest=<release\manifest.json> /O<release> installer.iss
; Installs for the current user (no admin) into %LOCALAPPDATA%\Programs\Kura: Kura keeps its settings and caches next
; to Kura.exe and the in-app updater swaps files there, so the folder has to be writable.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "."
#endif
#ifndef Manifest
  #define Manifest SourceDir + "\release\manifest.json"
#endif

[Setup]
AppId={{67c6d889-9279-4405-a19b-39d74bf00b9c}
AppName=Kura
AppVersion={#AppVersion}
AppVerName=Kura {#AppVersion}
AppPublisher=AruteOwO
AppPublisherURL=https://github.com/AruteOwO/kura
AppSupportURL=https://github.com/AruteOwO/kura/issues
AppUpdatesURL=https://github.com/AruteOwO/kura/releases
DefaultDirName={localappdata}\Programs\Kura
DefaultGroupName=Kura
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=Kura-Setup-{#AppVersion}
SetupIconFile={#SourceDir}\kura.ico
UninstallDisplayIcon={app}\Kura.exe
UninstallDisplayName=Kura
LicenseFile={#SourceDir}\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\Kura.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\kura_runtime\*"; DestDir: "{app}\kura_runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\fonts\*"; DestDir: "{app}\fonts"; Flags: ignoreversion
Source: "{#SourceDir}\anime_organizer.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
; the release installed here: the updater reads which organizer script it put down
Source: "{#Manifest}"; DestDir: "{app}"; DestName: "kura_installed.json"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Kura"; Filename: "{app}\Kura.exe"; AppUserModelID: "Kura.AnimeLibrary"
Name: "{autodesktop}\Kura"; Filename: "{app}\Kura.exe"; AppUserModelID: "Kura.AnimeLibrary"; Tasks: desktopicon

[Run]
Filename: "{app}\Kura.exe"; Description: "{cm:LaunchProgram,Kura}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; program files the updater or Kura put here after install (never your data)
Type: filesandordirs; Name: "{app}\kura_runtime"
Type: filesandordirs; Name: "{app}\kura_runtime.old-*"
Type: files; Name: "{app}\Kura.old-*.exe"
Type: filesandordirs; Name: "{app}\update"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\fonts"
Type: files; Name: "{app}\kura_installed.json"

[Code]
var
  RemoveData: Boolean;

function InitializeUninstall(): Boolean;
begin
  RemoveData := False;
  if not UninstallSilent then
    RemoveData := MsgBox('Also remove your Kura settings, sign-ins, lists and caches from this PC?' + #13#10#13#10 +
      'Choose No to keep them for a later reinstall. Your anime library folders are never touched.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  Result := True;
end;

procedure RemoveKuraData(Dir: String);
begin
  { only what Kura writes next to Kura.exe (the same list .gitignore keeps out of the repo), never the whole folder:
    someone may have installed into a folder of their own }
  DelTree(Dir + '\*.json', False, True, False);
  DelTree(Dir + '\*.bin', False, True, False);
  DelTree(Dir + '\*.log', False, True, False);
  DelTree(Dir + '\*.tmp', False, True, False);
  DelTree(Dir + '\cover_cache', True, True, True);
  DelTree(Dir + '\backups', True, True, True);
  DelTree(Dir + '\subtitle_backups', True, True, True);
  RemoveDir(Dir);   { only if nothing else is left in it }
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Kura');
  if (CurUninstallStep = usPostUninstall) and RemoveData then
    RemoveKuraData(ExpandConstant('{app}'));
end;
