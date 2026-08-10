#define AppName "Cloud Storage Server"
#define AppVersion "0.9.5"
#define AppPublisher "Cloud Storage"
#ifndef BuildRoot
#define BuildRoot "..\dist"
#endif

[Setup]
AppId={{C6C699F4-2F76-4D14-B7A4-A0B120362E1E}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\Cloud Storage\Server
DefaultGroupName=Cloud Storage
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\outputs
OutputBaseFilename=CloudStorage-Server-Setup-{#AppVersion}-windows-x64
SetupIconFile=..\assets\cloud-storage.ico
UninstallDisplayIcon={app}\Manager\CloudStorageServerManager.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=CloudStorageServerManager.exe,CloudStorageServerCore.exe,CloudStorageServerService.exe
RestartApplications=no
RestartIfNeededByRun=no
UsePreviousAppDir=yes
MinVersion=10.0.17763

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык Server Manager на рабочем столе"; GroupDescription: "Дополнительные ярлыки:"; Flags: checkedonce
Name: "privatefirewall"; Description: "Разрешить клиентский HTTPS и обнаружение сервера в частной сети"; GroupDescription: "Сеть Windows:"; Flags: checkedonce

[Dirs]
Name: "{commonappdata}\CloudStorage"; Permissions: admins-full system-full

[Files]
Source: "{#BuildRoot}\CloudStorageServerManager\*"; DestDir: "{app}\Manager"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildRoot}\CloudStorageServerCore\*"; DestDir: "{app}\CloudStorageServerCore"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildRoot}\CloudStorageServerService\*"; DestDir: "{app}\Service"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Cloud Storage Server Manager"; Filename: "{app}\Manager\CloudStorageServerManager.exe"
Name: "{autodesktop}\Cloud Storage Server Manager"; Filename: "{app}\Manager\CloudStorageServerManager.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CloudStorageServerCore\CloudStorageServerCore.exe"; Parameters: "--initialize-only"; StatusMsg: "Создаём защищённую базу сервера…"; Flags: runhidden waituntilterminated
Filename: "{app}\Service\CloudStorageServerService.exe"; Parameters: "--startup auto install"; StatusMsg: "Устанавливаем службу Cloud Storage…"; Flags: runhidden waituntilterminated; Check: not IsServerServiceInstalled
Filename: "{sys}\sc.exe"; Parameters: "config CloudStorageServerCore start= auto"; Flags: runhidden waituntilterminated
Filename: "{sys}\sc.exe"; Parameters: "failure CloudStorageServerCore reset= 86400 actions= restart/5000/restart/15000/restart/60000"; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Cloud Storage HTTPS (Private)"" dir=in action=allow protocol=TCP localport=8766 profile=private"; Flags: runhidden waituntilterminated; Tasks: privatefirewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Cloud Storage Discovery (Private)"" dir=in action=allow protocol=UDP localport=47777 profile=private"; Flags: runhidden waituntilterminated; Tasks: privatefirewall
Filename: "{sys}\sc.exe"; Parameters: "start CloudStorageServerCore"; StatusMsg: "Запускаем серверную службу…"; Flags: runhidden waituntilterminated
Filename: "{app}\Manager\CloudStorageServerManager.exe"; Description: "Открыть Cloud Storage Server Manager"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\net.exe"; Parameters: "stop CloudStorageServerCore /y"; Flags: runhidden waituntilterminated; RunOnceId: "StopCloudStorageServerCore"
Filename: "{app}\Service\CloudStorageServerService.exe"; Parameters: "remove"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "RemoveCloudStorageServerCore"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Cloud Storage HTTPS (Private)"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveCloudStorageHttpsFirewall"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Cloud Storage Discovery (Private)"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveCloudStorageDiscoveryFirewall"

[Code]
function IsServerServiceInstalled: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\sc.exe'), 'query CloudStorageServerCore', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure BackupServerData;
var
  DataPath, BackupPath, Stamp: String;
begin
  DataPath := ExpandConstant('{commonappdata}\CloudStorage');
  if not FileExists(DataPath + '\core.db') then
    Exit;
  Stamp := GetDateTimeString('yyyymmdd-hhnnss', '-', ':');
  BackupPath := DataPath + '\pre-update-' + Stamp;
  ForceDirectories(BackupPath);
  CopyFile(DataPath + '\core.db', BackupPath + '\core.db', False);
  if FileExists(DataPath + '\core-config.json') then
    CopyFile(DataPath + '\core-config.json', BackupPath + '\core-config.json', False);
  if FileExists(DataPath + '\core-secrets.json') then
    CopyFile(DataPath + '\core-secrets.json', BackupPath + '\core-secrets.json', False);
  if FileExists(DataPath + '\core.db-wal') then
    CopyFile(DataPath + '\core.db-wal', BackupPath + '\core.db-wal', False);
  if FileExists(DataPath + '\core.db-shm') then
    CopyFile(DataPath + '\core.db-shm', BackupPath + '\core.db-shm', False);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  Exec(ExpandConstant('{sys}\net.exe'), 'stop CloudStorageServerCore /y', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  BackupServerData;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataPath: String;
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
  begin
    DataPath := ExpandConstant('{commonappdata}\CloudStorage');
    if MsgBox('Удалить базу, пользователей, настройки и все файлы Cloud Storage Server?'#13#10#13#10 +
      'Это действие необратимо. По умолчанию серверные данные сохраняются.',
      mbConfirmation, MB_YESNO) = IDYES then
      if MsgBox('Последнее подтверждение: действительно удалить всё хранилище сервера?',
        mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataPath, True, True, True);
  end;
end;
