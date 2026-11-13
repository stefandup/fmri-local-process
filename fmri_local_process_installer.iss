; fMRI Local Process installer.
; Built by build.ps1, which invokes: ISCC.exe fmri_local_process_installer.iss /DMyAppVersion=<version>

#define MyAppName "fMRI Local Process"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#define MyAppPublisher "Stellenbosch University"
#define MyAppURL "https://www.sun.ac.za"
; Matches $AppName in build.ps1.
#define MyAppExeName "fmri_convert_to_bids.exe"
#define MyAppId "05C45EB1-8EDF-41EA-A79E-BC1A8E0645B3"
; Passed by build.ps1 from processing/cpac_docker.py's CPAC_IMAGE, so both pull the same tag.
#ifndef CpacImage
  #define CpacImage "fcpindi/c-pac:release-v1.8.7"
#endif

[Setup]
; Three "{" here, not two: the preprocessor substitutes {#MyAppId} before Inno's own "{{"
; escaping is evaluated, so "{{{#MyAppId}}" ends up as the required "{{<guid>}".
AppId={{{#MyAppId}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\FmriLocalProcess
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=FmriLocalProcessSetup-{#MyAppVersion}
; Relative to this script's directory (the repo root) -- keeps every build artifact under
; the one gitignored build_output/ tree.
OutputDir=build_output\installer
Compression=lzma
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "build_output\payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: desktopicon; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"
Name: pullcpac; Description: "Download the C-PAC Docker image now (10+ GB, needs Docker Desktop running)"; GroupDescription: "fmri_preprocess:"

[Icons]
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Code]
const
  WM_SETTINGCHANGE = $1A;
  SMTO_ABORTIFHUNG = $0002;

function SendMessageTimeoutA(hWnd: Integer; Msg: Integer; wParam: Integer; lParam: String;
  fuFlags: Integer; uTimeout: Integer; var lpdwResult: Integer): Integer;
  external 'SendMessageTimeoutA@user32.dll stdcall';

procedure EnvBroadcastChange();
var
  Res: Integer;
begin
  SendMessageTimeoutA(HWND_BROADCAST, WM_SETTINGCHANGE, 0, 'Environment', SMTO_ABORTIFHUNG, 5000, Res);
end;

// Current-user PATH only (matches PrivilegesRequired=lowest -- no HKLM access).
procedure EnvAddPath(Path: string);
var
  Paths: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths) then
    Paths := '';

  if Pos(';' + Uppercase(Path) + ';', ';' + Uppercase(Paths) + ';') > 0 then
    exit;

  if (Length(Paths) > 0) and (Paths[Length(Paths)] <> ';') then
    Paths := Paths + ';';
  Paths := Paths + Path + ';';

  RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths);
end;

procedure EnvRemovePath(Path: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths) then
    exit;

  P := Pos(';' + Uppercase(Path) + ';', ';' + Uppercase(Paths) + ';');
  if P = 0 then
    exit;

  Delete(Paths, P - 1, Length(Path) + 1);
  RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths);
end;

// The uninstall entry lives under HKCU only (PrivilegesRequired=lowest).
// Returns '' if no previous install of this AppId is found.
function GetUninstallString(): String;
var
  UninstallKey: String;
  UninstallString: String;
begin
  UninstallKey := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{' + '{#MyAppId}' + '}_is1';
  if not RegQueryStringValue(HKCU, UninstallKey, 'QuietUninstallString', UninstallString) then
    RegQueryStringValue(HKCU, UninstallKey, 'UninstallString', UninstallString);
  Result := UninstallString;
end;

function IsUpgrade(): Boolean;
begin
  Result := (GetUninstallString() <> '');
end;

// Pulls just the (quoted) exe path out of an uninstall command line, dropping any arguments.
function ExtractExePath(const CommandLine: String): String;
var
  ClosingQuote: Integer;
begin
  if (Length(CommandLine) > 0) and (CommandLine[1] = '"') then
  begin
    ClosingQuote := Pos('"', Copy(CommandLine, 2, Length(CommandLine) - 1));
    if ClosingQuote > 0 then
    begin
      Result := Copy(CommandLine, 2, ClosingQuote - 1);
      exit;
    end;
  end;
  Result := CommandLine;
end;

// Silently uninstalls the previous version first, so files the new version no longer ships
// don't linger in {app}.
procedure UninstallOldVersion();
var
  UninstallString: String;
  ResultCode: Integer;
begin
  UninstallString := GetUninstallString();
  if UninstallString = '' then
    exit;
  UninstallString := ExtractExePath(UninstallString);
  Exec(UninstallString, '/VERYSILENT /NORESTART /SUPPRESSMSGBOXES', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode);
end;

function DockerAnswers(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/c docker info >nul 2>&1', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

// Optional: fmri_preprocess would otherwise pull the image on its first run. Every step can
// be skipped, and a failure here never fails the install. Silent installs skip it
// (SuppressibleMsgBox returns the given default).
procedure PullCpacImage();
var
  ResultCode: Integer;
begin
  if SuppressibleMsgBox('fmri_preprocess runs C-PAC inside Docker. Downloading its image'#13#10
    + '{#CpacImage}'#13#10'needs 10+ GB of disk space and can take an hour or more.'#13#10#13#10
    + 'Download it now? Choose No to skip: it then downloads on the first fmri_preprocess run.',
    mbConfirmation, MB_YESNO, IDNO) <> IDYES then
    exit;

  while not DockerAnswers() do
    if SuppressibleMsgBox('Docker Desktop is not installed or not running.'#13#10#13#10
      + 'Install it from https://www.docker.com/products/docker-desktop/ or start it, wait '
      + 'until it says "Engine running", then choose Retry. Choose Cancel to skip the download.',
      mbError, MB_RETRYCANCEL, IDCANCEL) <> IDRETRY then
      exit;

  // Visible console window, so docker's own download progress shows.
  if not Exec(ExpandConstant('{cmd}'), '/c docker pull {#CpacImage}', '', SW_SHOW,
    ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    SuppressibleMsgBox('Downloading the C-PAC image failed (exit code ' + IntToStr(ResultCode)
      + '). The install itself is complete; the image downloads on the first fmri_preprocess '
      + 'run instead, or run:  docker pull {#CpacImage}', mbError, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    if IsUpgrade() then
      UninstallOldVersion();
  end
  else if CurStep = ssPostInstall then
  begin
    EnvAddPath(ExpandConstant('{app}'));
    EnvBroadcastChange();
    if WizardIsTaskSelected('pullcpac') then
      PullCpacImage();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    EnvRemovePath(ExpandConstant('{app}'));
    EnvBroadcastChange();
  end;
end;
