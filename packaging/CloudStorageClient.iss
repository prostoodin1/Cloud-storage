#define AppName "Cloud Storage Client"
#define AppVersion "0.5.0-alpha.3"
#define AppPublisher "Cloud Storage"
#define AppExeName "CloudStorageClient.exe"

[Setup]
AppId={{B7B6A410-25AF-47DC-96D6-B40F85E762A8}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\Cloud Storage Client
DefaultGroupName=Cloud Storage
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\outputs
OutputBaseFilename=CloudStorageClient-0.5.0a3-windows-x64-setup
SetupIconFile=..\assets\cloud-storage.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные ярлыки:"; Flags: unchecked

[Files]
Source: "..\dist\CloudStorageClient\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Cloud Storage Client"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Cloud Storage Client"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Запустить Cloud Storage Client"; Flags: nowait postinstall skipifsilent
