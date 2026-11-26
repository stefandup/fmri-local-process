; fMRI Local Process installer.
; Built by build.ps1, which invokes: ISCC.exe fmri_local_process_installer.iss /DMyAppVersion=<version>

#define MyAppName "fMRI Local Process"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#define MyAppPublisher "Stellenbosch University"
#define MyAppURL "https://www.sun.ac.za"
; Matches $ToolboxApp in build.ps1: the toolbox window with every tab. The command-line
; tools ($Apps in build.ps1) sit beside it in the toolbox folder, which is put on PATH.
#define MyAppExeName "fmri_toolbox.exe"
#define MyAppId "05C45EB1-8EDF-41EA-A79E-BC1A8E0645B3"
; Passed by build.ps1 from processing/cpac_docker.py's CPAC_IMAGE, so both pull the same tag.
#ifndef CpacImage
  #define CpacImage "fcpindi/c-pac:release-v1.8.7"
#endif
; Passed by build.ps1 from processing/deface.py's DEFACE_IMAGE: the image the setup builds.
#ifndef DefaceImage
  #define DefaceImage "philani_fmri_process:latest"
#endif
; Passed by build.ps1 from processing/mriqc.py's MRIQC_IMAGE: the toolbox's Scan quality tab.
#ifndef MriqcImage
  #define MriqcImage "nipreps/mriqc:24.0.2"
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

[Icons]
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: postinstall nowait skipifsilent

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

// ---- Docker and tools page --------------------------------------------------------------
// One checklist: Docker Desktop, the C-PAC image, the MRIQC image and the T1w defacer image. Anything already
// on this PC is shown as OK and greyed out (the defacer stays tickable, to rebuild/switch it).

function DockerAnswers(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/c docker info >nul 2>&1', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function DockerInstalled(): Boolean;
var
  ResultCode: Integer;
begin
  Result := FileExists(ExpandConstant('{commonpf64}\Docker\Docker\Docker Desktop.exe'))
    or (Exec(ExpandConstant('{cmd}'), '/c where docker >nul 2>&1', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) and (ResultCode = 0));
end;

function ImagePresent(Image: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/c docker image inspect ' + Image + ' >nul 2>&1',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

// Order of the -Defacer radio buttons; matches DefacerArg().
const
  DEFACER_FREESURFER = 0;
  DEFACER_LOCAL = 1;
  DEFACER_PYDEFACE = 2;

var
  ToolsPage: TWizardPage;
  ToolsList: TNewCheckListBox;
  DefaceFilesPage: TInputDirWizardPage;
  HaveDocker, DockerRunning, HaveCpac, HaveMriqc, HaveDefacer: Boolean;
  ItemDocker, ItemCpac, ItemMriqc, ItemDefacer: Integer;
  ItemRadio: array[0..2] of Integer;

function WantDockerInstall(): Boolean;
begin
  Result := (not HaveDocker) and ToolsList.Checked[ItemDocker];
end;

function WantCpac(): Boolean;
begin
  Result := (not HaveCpac) and ToolsList.Checked[ItemCpac];
end;

function WantMriqc(): Boolean;
begin
  Result := (not HaveMriqc) and ToolsList.Checked[ItemMriqc];
end;

function WantDefacer(): Boolean;
begin
  Result := ToolsList.Checked[ItemDefacer];
end;

function SelectedDefacer(): Integer;
var
  I: Integer;
begin
  Result := DEFACER_FREESURFER;
  for I := 0 to 2 do
    if ToolsList.Checked[ItemRadio[I]] then
      Result := I;
end;

procedure InitializeWizard();
var
  Intro, Caption: String;
begin
  HaveDocker := DockerInstalled();
  DockerRunning := HaveDocker and DockerAnswers();
  HaveCpac := DockerRunning and ImagePresent('{#CpacImage}');
  HaveMriqc := DockerRunning and ImagePresent('{#MriqcImage}');
  HaveDefacer := DockerRunning and ImagePresent('{#DefaceImage}');

  Intro := 'Preprocessing (C-PAC), scan quality checks (MRIQC) and T1w defacing run in Docker. Tick what to set '
    + 'up now; everything can be skipped and done later by re-running this installer.';
  if HaveDocker and not DockerRunning then
    Intro := Intro + ' Docker Desktop is not running, so it can''t tell which images are '
      + 'already downloaded: ticked downloads are skipped if they are.';
  ToolsPage := CreateCustomPage(wpSelectTasks, 'Docker and tools',
    'What should be set up for preprocessing and defacing?');

  ToolsList := TNewCheckListBox.Create(ToolsPage);
  ToolsList.Parent := ToolsPage.Surface;
  ToolsList.Top := ScaleY(56);
  ToolsList.Width := ToolsPage.SurfaceWidth;
  ToolsList.Height := ToolsPage.SurfaceHeight - ScaleY(56);
  with TNewStaticText.Create(ToolsPage) do
  begin
    Parent := ToolsPage.Surface;
    Width := ToolsPage.SurfaceWidth;
    AutoSize := False;
    WordWrap := True;
    Height := ScaleY(52);
    Caption := Intro;
  end;

  if HaveDocker then
    Caption := 'Docker Desktop: installed - OK'
  else
    Caption := 'Install Docker Desktop (~600 MB download, asks for administrator rights)';
  ItemDocker := ToolsList.AddCheckBox(Caption, '', 0, not HaveDocker, not HaveDocker,
    False, True, nil);

  if HaveCpac then
    Caption := 'C-PAC ({#CpacImage}): downloaded - OK'
  else
    Caption := 'Download C-PAC ({#CpacImage}, ~15 GB; 30-90+ minutes)';
  ItemCpac := ToolsList.AddCheckBox(Caption, '', 0, not HaveCpac, not HaveCpac, False, True, nil);

  if HaveMriqc then
    Caption := 'MRIQC ({#MriqcImage}): downloaded - OK'
  else
    Caption := 'Download MRIQC ({#MriqcImage}, ~5 GB; checks scan quality before preprocessing)';
  ItemMriqc := ToolsList.AddCheckBox(Caption, '', 0, not HaveMriqc, not HaveMriqc, False, True,
    nil);

  if HaveDefacer then
    Caption := 'T1w defacer ({#DefaceImage}): built - OK. Tick to rebuild it with:'
  else
    Caption := 'Set up the T1w defacer ({#DefaceImage}, built on C-PAC) with:';
  ItemDefacer := ToolsList.AddCheckBox(Caption, '', 0, not HaveDefacer, True, False, True, nil);
  ItemRadio[DEFACER_FREESURFER] := ToolsList.AddRadioButton(
    'FreeSurfer mri_deface, downloaded (recommended; extra ~5 GB, 30-90+ minutes)', '', 1,
    True, True, nil);
  ItemRadio[DEFACER_LOCAL] := ToolsList.AddRadioButton(
    'FreeSurfer mri_deface from a folder I already have (chosen on the next page)', '', 1,
    False, True, nil);
  ItemRadio[DEFACER_PYDEFACE] := ToolsList.AddRadioButton(
    'pydeface (small and quick, but worked less well on our scans)', '', 1, False, True, nil);

  DefaceFilesPage := CreateInputDirPage(ToolsPage.ID, 'Your FreeSurfer files',
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
  if PageID = DefaceFilesPage.ID then
    Result := not (WantDefacer() and (SelectedDefacer() = DEFACER_LOCAL));
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

// The setup script's -Defacer value: 'none' downloads only C-PAC.
function DefacerArg(): String;
begin
  if not WantDefacer() then
    Result := 'none'
  else
    case SelectedDefacer() of
      DEFACER_FREESURFER: Result := 'freesurfer';
      DEFACER_LOCAL: Result := 'local';
    else
      Result := 'pydeface';
    end;
end;

function DownloadEstimate(): String;
begin
  if HaveCpac then
    Result := '- C-PAC: already downloaded'
  else if WantCpac() or WantDefacer() then
    Result := '- C-PAC ({#CpacImage}): ~15 GB unpacked, 30-90+ minutes (skipped if already '
      + 'downloaded)'
  else
    Result := '- C-PAC: skipped';
  if WantMriqc() then
    Result := Result + #13#10'- MRIQC ({#MriqcImage}): ~5 GB, 15-45+ minutes (skipped if '
      + 'already downloaded)';
  if WantDefacer() then
    case SelectedDefacer() of
      DEFACER_FREESURFER: Result := Result + #13#10'- FreeSurfer: another ~5 GB download, '
        + '30-90+ minutes; only mri_deface is kept, the rest is deleted again';
      DEFACER_LOCAL: Result := Result + #13#10'- mri_deface from your folder: a few minutes';
      DEFACER_PYDEFACE: Result := Result + #13#10'- pydeface: a few MB, about a minute';
    end;
end;

// A trailing backslash before the closing quote would escape it on the command line.
function QuotedDir(Dir: String): String;
begin
  if (Length(Dir) > 0) and (Dir[Length(Dir)] = '\') then
    Dir := Dir + '.';
  Result := '"' + Dir + '"';
end;

// winget (built into Windows 10/11) downloads and installs Docker Desktop; it asks for
// administrator rights itself. Without winget, the download page is opened instead.
procedure InstallDocker();
var
  ResultCode: Integer;
begin
  WizardForm.StatusLabel.Caption := 'Installing Docker Desktop (progress in its own window)...';
  if Exec('winget.exe', 'install -e --id Docker.DockerDesktop --accept-package-agreements '
    + '--accept-source-agreements', '', SW_SHOW, ewWaitUntilTerminated, ResultCode)
    and (ResultCode = 0) then
  begin
    SuppressibleMsgBox('Docker Desktop is installed. Start it now from the Start menu, accept '
      + 'its terms and wait until it says "Engine running" (the first start can take a few '
      + 'minutes, and may ask you to restart Windows).', mbInformation, MB_OK, IDOK);
    exit;
  end;
  SuppressibleMsgBox('Docker Desktop could not be installed automatically. The download page '
    + 'opens now: install it, start it and wait for "Engine running".', mbError, MB_OK, IDOK);
  ShellExec('open', 'https://www.docker.com/products/docker-desktop/', '', '', SW_SHOW,
    ewNoWait, ResultCode);
end;

// After the files are installed: every step can be skipped, and a failure never fails the
// install. Silent installs skip it (SuppressibleMsgBox returns the given default).
procedure SetupDockerImages();
var
  ResultCode: Integer;
  Script, Params: String;
begin
  Script := ExpandConstant('{app}\docker\setup_docker_image.ps1');
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + Script + '" -Defacer ' + DefacerArg()
    + ' -CpacImage "{#CpacImage}" -Image "{#DefaceImage}"';
  if WantMriqc() then
    Params := Params + ' -Mriqc -MriqcImage "{#MriqcImage}"';
  // C-PAC already here or unticked: skip it (a defacer is built on it, so not then).
  if (not WantCpac()) and (not WantDefacer()) then
    Params := Params + ' -NoCpac';
  if WantDefacer() and (SelectedDefacer() = DEFACER_LOCAL) then
    Params := Params + ' -DefaceFiles ' + QuotedDir(DefaceFilesPage.Values[0]);

  if SuppressibleMsgBox('Docker setup will now download:'#13#10#13#10 + DownloadEstimate()
    + #13#10#13#10'This can take several hours on a slow connection. Keep the PC awake, on '
    + 'power and online. A PowerShell window shows the progress of each download.'#13#10#13#10
    + 'Start now? Choose No to skip; you can run it later with:'#13#10
    + 'powershell ' + Params, mbConfirmation, MB_YESNO, IDNO) <> IDYES then
    exit;

  while not DockerAnswers() do
    if SuppressibleMsgBox('Docker Desktop is not running.'#13#10#13#10
      + 'Start it from the Start menu, wait until it says "Engine running", then choose '
      + 'Retry. If it needs Windows restarted first, choose Cancel and re-run this installer '
      + 'afterwards.', mbError, MB_RETRYCANCEL, IDCANCEL) <> IDRETRY then
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
    if WantDockerInstall() then
      InstallDocker();
    // A defacer image is built on C-PAC, so the script also downloads C-PAC when needed.
    if WantCpac() or WantDefacer() or WantMriqc() then
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
