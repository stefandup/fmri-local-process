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
; Passed by build.ps1 from processing/deface.py's DEFACE_IMAGE: the image the setup builds.
#ifndef DefaceImage
  #define DefaceImage "philani_fmri_process:latest"
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
Name: dockersetup; Description: "Set up Docker now: download C-PAC and build the T1w defacer (10+ GB, can take hours; needs Docker Desktop running)"; GroupDescription: "Docker (fmri_preprocess, defacing):"

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

// ---- Defacer choice (shown only when the dockersetup task is ticked) ----------------------
// Order matches DefacerArg(): the -Defacer values of docker\setup_docker_image.ps1.
const
  DEFACER_FREESURFER = 0;
  DEFACER_LOCAL = 1;
  DEFACER_PYDEFACE = 2;
  DEFACER_NONE = 3;

var
  DefacerPage: TInputOptionWizardPage;
  DefaceFilesPage: TInputDirWizardPage;

procedure InitializeWizard();
begin
  DefacerPage := CreateInputOptionPage(wpSelectTasks, 'T1w defacing',
    'How should T1w images have their faces removed?',
    'The defacer is built into a local Docker image ({#DefaceImage}) on top of C-PAC, '
    + 'after the files are installed. Re-run the installer to change it later.',
    True, False);
  DefacerPage.Add('FreeSurfer mri_deface, downloaded (recommended). Extra ~5 GB download: '
    + 'expect 30-90+ minutes on top of C-PAC.');
  DefacerPage.Add('FreeSurfer mri_deface from files I already have (a FreeSurfer home, or '
    + 'mri_deface + its 2 templates). A few minutes.');
  DefacerPage.Add('pydeface. Small and quick to set up, but it worked less well on our scans.');
  DefacerPage.Add('No defacer: only download C-PAC. T1w images are then not defaced '
    + '(unless --freesurfer is given).');
  DefacerPage.SelectedValueIndex := DEFACER_FREESURFER;

  DefaceFilesPage := CreateInputDirPage(DefacerPage.ID, 'Your mri_deface files',
    'Where are mri_deface and its templates?',
    'Pick a FreeSurfer home (bin\mri_deface, average\talairach_mixed_with_skull.gca, '
    + 'average\face.gca) or one folder holding those three files. mri_deface must be the '
    + 'Linux version: it runs inside Docker.',
    False, '');
  DefaceFilesPage.Add('');
  DefaceFilesPage.Values[0] := GetEnv('FREESURFER_HOME');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = DefacerPage.ID then
    Result := not WizardIsTaskSelected('dockersetup')
  else if PageID = DefaceFilesPage.ID then
    Result := (not WizardIsTaskSelected('dockersetup'))
      or (DefacerPage.SelectedValueIndex <> DEFACER_LOCAL);
end;

// FreeSurfer-home layout (<SubFolder>\<Name>) or flat (<Name>), as the setup script accepts.
function HasDefaceFile(Folder, Name, SubFolder: String): Boolean;
begin
  Result := FileExists(AddBackslash(Folder) + SubFolder + '\' + Name)
    or FileExists(AddBackslash(Folder) + Name);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Folder: String;
begin
  Result := True;
  if CurPageID = DefaceFilesPage.ID then
  begin
    Folder := DefaceFilesPage.Values[0];
    if not (HasDefaceFile(Folder, 'mri_deface', 'bin')
      and HasDefaceFile(Folder, 'talairach_mixed_with_skull.gca', 'average')
      and HasDefaceFile(Folder, 'face.gca', 'average')) then
    begin
      MsgBox('mri_deface, talairach_mixed_with_skull.gca and face.gca were not all found in'#13#10
        + Folder + #13#10'(nor in its bin\ and average\ subfolders).', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function DefacerArg(): String;
begin
  case DefacerPage.SelectedValueIndex of
    DEFACER_FREESURFER: Result := 'freesurfer';
    DEFACER_LOCAL: Result := 'local';
    DEFACER_PYDEFACE: Result := 'pydeface';
  else
    Result := 'none';
  end;
end;

function DownloadEstimate(): String;
begin
  Result := '- C-PAC ({#CpacImage}): ~15 GB unpacked, 30-90+ minutes (skipped if already '
    + 'downloaded)';
  if DefacerPage.SelectedValueIndex = DEFACER_FREESURFER then
    Result := Result + #13#10'- FreeSurfer: another ~5 GB download, 30-90+ minutes; only '
      + 'mri_deface is kept, the rest is deleted again'
  else if DefacerPage.SelectedValueIndex = DEFACER_PYDEFACE then
    Result := Result + #13#10'- pydeface: a few MB, about a minute';
end;

// A trailing backslash before the closing quote would escape it on the command line.
function QuotedDir(Dir: String): String;
begin
  if (Length(Dir) > 0) and (Dir[Length(Dir)] = '\') then
    Dir := Dir + '.';
  Result := '"' + Dir + '"';
end;

// Optional, after the files are installed: every step can be skipped, and a failure never
// fails the install. Silent installs skip it (SuppressibleMsgBox returns the given default).
procedure SetupDockerImages();
var
  ResultCode: Integer;
  Script, Params: String;
begin
  Script := ExpandConstant('{app}\docker\setup_docker_image.ps1');
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + Script + '" -Defacer ' + DefacerArg()
    + ' -CpacImage "{#CpacImage}" -Image "{#DefaceImage}"';
  if DefacerPage.SelectedValueIndex = DEFACER_LOCAL then
    Params := Params + ' -DefaceFiles ' + QuotedDir(DefaceFilesPage.Values[0]);

  if SuppressibleMsgBox('Docker setup will now download:'#13#10#13#10 + DownloadEstimate()
    + #13#10#13#10'This can take several hours on a slow connection. Keep the PC awake, on '
    + 'power and online. A PowerShell window shows the progress of each download.'#13#10#13#10
    + 'Start now? Choose No to skip; you can run it later with:'#13#10
    + 'powershell ' + Params, mbConfirmation, MB_YESNO, IDNO) <> IDYES then
    exit;

  while not DockerAnswers() do
    if SuppressibleMsgBox('Docker Desktop is not installed or not running.'#13#10#13#10
      + 'Install it from https://www.docker.com/products/docker-desktop/ or start it, wait '
      + 'until it says "Engine running", then choose Retry. Choose Cancel to skip.',
      mbError, MB_RETRYCANCEL, IDCANCEL) <> IDRETRY then
      exit;

  WizardForm.StatusLabel.Caption := 'Setting up Docker (progress in the PowerShell window)...';
  // Visible window: the script's overall progress bar and docker's per-layer download bars.
  if not Exec('powershell.exe', Params + ' -PauseOnError', '', SW_SHOW,
    ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    SuppressibleMsgBox('Docker setup did not finish (exit code ' + IntToStr(ResultCode)
      + '). The install itself is complete. Re-run the setup later with:'#13#10
      + 'powershell ' + Params, mbError, MB_OK, IDOK);
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
    if WizardIsTaskSelected('dockersetup') then
      SetupDockerImages();
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
