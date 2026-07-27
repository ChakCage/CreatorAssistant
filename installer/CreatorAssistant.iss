#ifndef Profile
  #error Profile must be developer, commercial, or commercial-staging
#endif
#ifndef SourceDir
  #error SourceDir is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installers"
#endif

#if Profile == "developer"
  #define ProductName "Creator Assistant Developer"
  #define AppIdValue "{{83C2DB8C-AFB5-4A4D-AEB8-C995B7069F2B}"
  #define InstallLeaf "Developer"
  #define ExecutableName "CreatorAssistant-Developer.exe"
  #define SetupBase "CreatorAssistant-Developer-Setup-" + AppVersion
  #define DataLeaf "Developer"
#elif Profile == "commercial"
  #define ProductName "Creator Assistant"
  #define AppIdValue "{{3A858CC2-F9F4-46CB-BEE5-2906DE77D7C7}"
  #define InstallLeaf "Commercial"
  #define ExecutableName "CreatorAssistant.exe"
  #define SetupBase "CreatorAssistant-Commercial-Setup-" + AppVersion
  #define DataLeaf "Commercial"
#elif Profile == "commercial-staging"
  #define ProductName "Creator Assistant Commercial Staging"
  #define AppIdValue "{{421F6AE7-30A9-4E58-A6F8-F0C332E01AEF}"
  #define InstallLeaf "Commercial-Staging"
  #define ExecutableName "CreatorAssistant-Commercial-Staging.exe"
  #define SetupBase "CreatorAssistant-Commercial-Staging-Setup-" + AppVersion
  #define DataLeaf "CommercialStaging"
#else
  #error Unsupported Profile
#endif

[Setup]
AppId={#AppIdValue}
AppName={#ProductName}
AppVersion={#AppVersion}
AppPublisher=Creator Assistant
DefaultDirName={localappdata}\Programs\CreatorAssistant\{#InstallLeaf}
DefaultGroupName={#ProductName}
UninstallDisplayName={#ProductName}
UninstallDisplayIcon={app}\{#ExecutableName}
OutputDir={#OutputDir}
OutputBaseFilename={#SetupBase}
Compression=lzma2/ultra64
SolidCompression=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupMutex=CreatorAssistant-Installer-Global
AppMutex=CreatorAssistant-{#Profile}
CloseApplications=yes
RestartApplications=yes
UsePreviousAppDir=yes
UsePreviousTasks=yes
DisableProgramGroupPage=yes
WizardStyle=modern
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName={#ProductName}
VersionInfoDescription={#ProductName} Installer
VersionInfoCompany=Creator Assistant
VersionInfoCopyright=Creator Assistant

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\docs\beta-quick-start-ru.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#ProductName}"; Filename: "{app}\{#ExecutableName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#ProductName}"; Filename: "{app}\{#ExecutableName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#ExecutableName}"; Description: "Запустить {#ProductName}"; Flags: nowait postinstall skipifsilent
Filename: "notepad.exe"; Parameters: """{app}\docs\beta-quick-start-ru.md"""; Description: "Открыть краткую инструкцию"; Flags: postinstall skipifsilent shellexec

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ConfigDir, LocalDir, MessageText: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;

  ConfigDir := ExpandConstant('{userappdata}\CreatorAssistant\{#DataLeaf}');
  LocalDir := ExpandConstant('{localappdata}\CreatorAssistant\{#DataLeaf}');
  MessageText :=
    'Программа удалена. Пользовательские настройки и данные сохранены.' + #13#10 + #13#10 +
    'Также удалить настройки и данные Creator Assistant этой редакции?' + #13#10 + #13#10 +
    ConfigDir + #13#10 + LocalDir + #13#10 + #13#10 +
    'Общие модели Ollama/Whisper и внешние проекты удалены не будут.';
  if UninstallSilent and (Pos('/DELETEUSERDATA=1', Uppercase(GetCmdTail)) = 0) then
    exit;
  if UninstallSilent then
  begin
    DelTree(ConfigDir, True, True, True);
    DelTree(LocalDir, True, True, True);
    exit;
  end;
  if MsgBox(MessageText, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    DelTree(ConfigDir, True, True, True);
    DelTree(LocalDir, True, True, True);
  end;
end;
