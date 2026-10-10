; Binggo Windows 安装程序（需先运行 packaging/windows/build.ps1）
; 正式构建必须通过 ISCC /DAppVersion=x.y.z 注入版本（SSOT: src/app_paths.__version__）

#define AppName "Binggo"
#define AppPublisher "bilibili_binggo"
#define AppURL "https://github.com/luovicter-collab/bilibinggo"
#define AppExeName "Binggo.exe"
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{A4B8F2E1-6C3D-4E9A-9B12-7D5E8F0A1C2B}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=Binggo-Setup-win64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}
PrivilegesRequired=lowest
AppMutex=Global\BilibiliBinggoDashboard
; Never let Restart Manager forcibly terminate multi-Profile mutations.
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; 简体中文：若本机 Inno 已安装 ChineseSimplified.isl，可取消下一行注释
; Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项:"; Flags: unchecked

[Files]
Source: "..\..\dist\Binggo\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
function BinggoStopped(): Boolean;
var
  Locator, Services, Processes: Variant;
begin
  Result := False;
  try
    { Read-only process inventory: any installed/portable Binggo instance,
      including a service without its supervisor, prevents replacement. }
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Services := Locator.ConnectServer('', 'root\CIMV2');
    Processes := Services.ExecQuery('SELECT ProcessId FROM Win32_Process WHERE Name="{#AppExeName}"');
    Result := Processes.Count = 0;
  except
    { Unknown process state is not permission to overwrite a running app. }
    Log('Cannot verify Binggo process state: ' + GetExceptionMessage);
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := BinggoStopped();
  if not Result then
    MsgBox('Binggo is still running, or its process state cannot be verified. Please safely exit EVERY account using its tray menu, then run Setup again. No account process will be forcibly stopped.', mbError, MB_OK);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not BinggoStopped() then
    Result := 'Please safely exit all Binggo accounts from their tray menus before installing. If process verification fails, installation is blocked to protect running tasks and databases.';
end;

function InitializeUninstall(): Boolean;
begin
  Result := BinggoStopped();
  if not Result then
    MsgBox('Please safely exit all Binggo accounts before uninstalling. No account process will be forcibly stopped.', mbError, MB_OK);
end;
