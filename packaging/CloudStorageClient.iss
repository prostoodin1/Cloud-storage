#define AppName "Cloud Storage Client"
#define AppVersion "0.9.21"
#define AppPublisher "Cloud Storage"
#define AppExeName "CloudStorageClient.exe"
#define WinFspMsi "winfsp-2.1.25156.msi"
#ifndef BuildRoot
#define BuildRoot "..\dist"
#endif

[Setup]
AppId={{B7B6A410-25AF-47DC-96D6-B40F85E762A8}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\Cloud Storage\Client
DefaultGroupName=Cloud Storage
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\outputs
OutputBaseFilename=CloudStorage-Client-Setup-{#AppVersion}-windows-x64
SetupIconFile=..\assets\cloud-storage.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=CloudStorageClient.exe,CloudStorageDrive.exe
RestartApplications=no
RestartIfNeededByRun=no
UsePreviousAppDir=yes
MinVersion=10.0.17763
AppMutex=CloudStorageClientSetup-SingleInstance

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные ярлыки:"; Flags: unchecked
Name: "autostart"; Description: "Запускать клиент и подключать диски после входа в Windows"; GroupDescription: "Интеграция с Windows:"; Flags: checkedonce

[Files]
Source: "{#BuildRoot}\CloudStorageClient\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "third_party\{#WinFspMsi}"; DestDir: "{tmp}"; Flags: deleteafterinstall dontcopy
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Cloud Storage Client"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Cloud Storage Client"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CloudStorageClient"; ValueData: """{app}\{#AppExeName}"" --minimized"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{sys}\msiexec.exe"; Parameters: "/i ""{tmp}\{#WinFspMsi}"" /qn /norestart"; StatusMsg: "Устанавливаем компонент диска WinFsp…"; Flags: waituntilterminated; Check: not IsWinFspInstalled
Filename: "{app}\{#AppExeName}"; Description: "Запустить Cloud Storage Client"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM CloudStorageDrive.exe /T /F"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopCloudStorageDrive"
Filename: "{sys}\taskkill.exe"; Parameters: "/IM CloudStorageClient.exe /T /F"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopCloudStorageClient"

[Code]
function IsWinFspInstalled: Boolean;
begin
  Result := RegKeyExists(HKLM32, 'SOFTWARE\WinFsp');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    ExtractTemporaryFile('{#WinFspMsi}');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataPath: String;
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
  begin
    DataPath := ExpandConstant('{localappdata}\CloudStorageClient');
    if MsgBox('Удалить локальные настройки, токены и кэш Cloud Storage Client?'#13#10#13#10 +
      'Файлы на сервере удалены не будут. По умолчанию эти данные сохраняются.',
      mbConfirmation, MB_YESNO) = IDYES then
      DelTree(DataPath, True, True, True);
  end;
end;
