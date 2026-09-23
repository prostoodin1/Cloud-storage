#define AppName "Cloud Storage Server"
#define AppVersion "0.10.3"
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
CloseApplicationsFilter=CloudStorageServerManager.exe,CloudStorageContainerManager.exe,CloudStorageServerCore.exe,CloudStorageLegacyCore.exe,CloudStorageServerService.exe
RestartApplications=no
RestartIfNeededByRun=no
UsePreviousAppDir=yes
MinVersion=10.0.17763
AppMutex=CloudStorageServerSetup-SingleInstance

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык Server Manager на рабочем столе"; GroupDescription: "Ярлыки (выберите нужное):"; Flags: unchecked
Name: "privatefirewall"; Description: "Разрешить клиентский HTTPS и обнаружение только из локальной подсети"; GroupDescription: "Сеть Windows:"; Flags: checkedonce

[Dirs]
Name: "{commonappdata}\CloudStorage"; Permissions: admins-full system-full

[Files]
Source: "{#BuildRoot}\CloudStorageServerManager\*"; DestDir: "{app}\Manager"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildRoot}\CloudStorageContainerManager\*"; DestDir: "{app}\ContainerManager"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildRoot}\CloudStorageServerCore\*"; DestDir: "{app}\CloudStorageServerCore"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildRoot}\CloudStorageLegacyCore\*"; DestDir: "{app}\CloudStorageLegacyCore"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BuildRoot}\CloudStorageServerService\*"; DestDir: "{app}\Service"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Cloud Storage Server Manager"; Filename: "{app}\Manager\CloudStorageServerManager.exe"
Name: "{group}\Cloud Storage Container Manager"; Filename: "{app}\ContainerManager\CloudStorageContainerManager.exe"
Name: "{autodesktop}\Cloud Storage Server Manager"; Filename: "{app}\Manager\CloudStorageServerManager.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CloudStorageServerCore\CloudStorageServerCore.exe"; Parameters: "--initialize-only"; StatusMsg: "Создаём защищённую базу сервера…"; Flags: runhidden waituntilterminated
Filename: "{app}\Service\CloudStorageServerService.exe"; Parameters: "--startup auto install"; StatusMsg: "Устанавливаем службу Cloud Storage…"; Flags: runhidden waituntilterminated
Filename: "{sys}\sc.exe"; Parameters: "config CloudStorageServerCore start= auto"; Flags: runhidden waituntilterminated
Filename: "{sys}\sc.exe"; Parameters: "failure CloudStorageServerCore reset= 86400 actions= restart/5000/restart/15000/restart/60000"; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Cloud Storage HTTPS (Private)"""; Flags: runhidden waituntilterminated; Tasks: privatefirewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Cloud Storage Discovery (Private)"""; Flags: runhidden waituntilterminated; Tasks: privatefirewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Cloud Storage HTTPS (Local subnet)"""; Flags: runhidden waituntilterminated; Tasks: privatefirewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Cloud Storage Discovery (Local subnet)"""; Flags: runhidden waituntilterminated; Tasks: privatefirewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Cloud Storage HTTPS (Local subnet)"" dir=in action=allow protocol=TCP localport=8766 profile=any remoteip=localsubnet"; Flags: runhidden waituntilterminated; Tasks: privatefirewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Cloud Storage Discovery (Local subnet)"" dir=in action=allow protocol=UDP localport=47777 profile=any remoteip=localsubnet"; Flags: runhidden waituntilterminated; Tasks: privatefirewall
Filename: "{sys}\sc.exe"; Parameters: "start CloudStorageServerCore"; StatusMsg: "Запускаем серверную службу…"; Flags: runhidden waituntilterminated
Filename: "{app}\Manager\CloudStorageServerManager.exe"; Description: "Открыть Cloud Storage Server Manager"; Flags: nowait postinstall skipifsilent runascurrentuser shellexec

[UninstallRun]
Filename: "{sys}\net.exe"; Parameters: "stop CloudStorageServerCore /y"; Flags: runhidden waituntilterminated; RunOnceId: "StopCloudStorageServerCore"
Filename: "{app}\Service\CloudStorageServerService.exe"; Parameters: "remove"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "RemoveCloudStorageServerCore"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Cloud Storage HTTPS (Private)"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveCloudStorageHttpsFirewall"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Cloud Storage Discovery (Private)"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveCloudStorageDiscoveryFirewall"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Cloud Storage HTTPS (Local subnet)"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveCloudStorageHttpsLocalSubnetFirewall"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Cloud Storage Discovery (Local subnet)"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveCloudStorageDiscoveryLocalSubnetFirewall"

[Code]
var
  UpgradeServiceTemporarilyDisabled: Boolean;
  LastBackupError: String;

function IsServerServiceInstalled: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\sc.exe'), 'query CloudStorageServerCore', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure RestoreServerService;
var
  ResultCode: Integer;
begin
  if not UpgradeServiceTemporarilyDisabled then
    Exit;
  Exec(ExpandConstant('{sys}\sc.exe'), 'config CloudStorageServerCore start= auto', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\sc.exe'),
    'failure CloudStorageServerCore reset= 86400 actions= restart/5000/restart/15000/restart/60000', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\sc.exe'), 'start CloudStorageServerCore', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  UpgradeServiceTemporarilyDisabled := False;
end;

procedure StopImage(ImageName: String);
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM "' + ImageName + '" /T /F', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure StopServerComponents;
var
  ResultCode, Attempt: Integer;
begin
  { Managers can restart the Core, so close them before stopping the service. }
  StopImage('CloudStorageServerManager.exe');
  StopImage('CloudStorageContainerManager.exe');
  { A forced stop used to trigger the configured recovery action and restart the
    old wrapper while its secrets were being copied. Disable start and recovery
    for the short protected update window. DeinitializeSetup restores both if
    installation is cancelled or fails before the normal [Run] stage. }
  if IsServerServiceInstalled then begin
    Exec(ExpandConstant('{sys}\sc.exe'),
      'failure CloudStorageServerCore reset= 0 actions= ""', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec(ExpandConstant('{sys}\sc.exe'),
      'config CloudStorageServerCore start= disabled', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
    UpgradeServiceTemporarilyDisabled := True;
  end;
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop CloudStorageServerCore', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\net.exe'), 'stop CloudStorageServerCore /y', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  { Older installations may have a direct/legacy Core outside SCM. Repeat the
    termination briefly so handles are closed before the protected backup. }
  for Attempt := 1 to 3 do begin
    StopImage('CloudStorageServerCore.exe');
    StopImage('CloudStorageLegacyCore.exe');
    StopImage('CloudStorageServerService.exe');
    Sleep(500);
  end;
end;

procedure RepairServerSecretPermissions;
var
  SecretPath: String;
  ResultCode: Integer;
begin
  SecretPath := ExpandConstant('{commonappdata}\CloudStorage\core-secrets.json');
  if FileExists(SecretPath) then
    Exec(ExpandConstant('{sys}\icacls.exe'),
      '"' + SecretPath + '" /inheritance:r /grant:r *S-1-5-18:(F) *S-1-5-32-544:(F)', '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function CopyFileWithRetry(SourcePath, DestinationPath: String;
  SourceMayDisappear: Boolean): Boolean;
var
  Attempt: Integer;
begin
  Result := False;
  LastBackupError := '';
  for Attempt := 1 to 40 do begin
    if not FileExists(SourcePath) then begin
      Result := SourceMayDisappear;
      Exit;
    end;
    if CopyFile(SourcePath, DestinationPath, False) then begin
      Result := True;
      Exit;
    end;
    Sleep(250);
  end;
  LastBackupError := SysErrorMessage(DLLGetLastError);
end;

function BackupServerData: String;
var
  DataPath, BackupPath, Stamp: String;
begin
  Result := '';
  DataPath := ExpandConstant('{commonappdata}\CloudStorage');
  if not FileExists(DataPath + '\core.db') then
    Exit;
  Stamp := GetDateTimeString('yyyymmdd-hhnnss', '-', ':');
  BackupPath := DataPath + '\pre-update-' + Stamp;
  if not ForceDirectories(BackupPath) then begin
    Result := 'Не удалось создать резервную копию. Обновление остановлено: ' + BackupPath;
    Exit;
  end;
  if not CopyFileWithRetry(DataPath + '\core.db', BackupPath + '\core.db', False) then begin
    Result := 'Не удалось скопировать базу. Обновление остановлено; проверьте место и права доступа.';
    Exit;
  end;
  if FileExists(DataPath + '\core-config.json') then
    if not CopyFileWithRetry(DataPath + '\core-config.json', BackupPath + '\core-config.json', False) then begin
      Result := 'Не удалось сохранить настройки сервера. Обновление остановлено.';
      Exit;
    end;
  if FileExists(DataPath + '\core-secrets.json') then
    if not CopyFileWithRetry(DataPath + '\core-secrets.json', BackupPath + '\core-secrets.json', False) then begin
      Result := 'Не удалось сохранить ключи сервера. Обновление остановлено.'#13#10 +
        'Причина Windows: ' + LastBackupError;
      Exit;
    end;
  if FileExists(DataPath + '\core.db-wal') then
    if not CopyFileWithRetry(DataPath + '\core.db-wal', BackupPath + '\core.db-wal', True) then begin
      Result := 'Не удалось сохранить журнал базы. Обновление остановлено.';
      Exit;
    end;
  if FileExists(DataPath + '\core.db-shm') then
    if not CopyFileWithRetry(DataPath + '\core.db-shm', BackupPath + '\core.db-shm', True) then begin
      Result := 'Не удалось сохранить состояние базы. Обновление остановлено.';
      Exit;
    end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  StopServerComponents;
  { The native Go supervisor validates process identity and no longer trusts
    stale numeric PID files from 0.9.x. Remove the legacy lock after stopping. }
  DeleteFile(ExpandConstant('{commonappdata}\CloudStorage\core.pid'));
  RepairServerSecretPermissions;
  Result := BackupServerData;
  if Result <> '' then
    RestoreServerService;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssDone then
    UpgradeServiceTemporarilyDisabled := False;
end;

procedure DeinitializeSetup;
begin
  RestoreServerService;
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
