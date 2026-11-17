#Requires -Version 7
param(
    [switch]$Full,
    [switch]$Exe,
    [switch]$Inno,
    [switch]$Review,
    # -Review only: build from this saved plan instead of opening the picker.
    [string]$Plan
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Every build artifact (PyInstaller work files, its onedir output, the assembled installer
# payload, and the compiled installer) lives under this one gitignored root.
$BuildOutput = Join-Path $PSScriptRoot "build_output"
$WorkPath = Join-Path $BuildOutput "work"
$DistPath = Join-Path $BuildOutput "dist"
$PayloadPath = Join-Path $BuildOutput "payload"
# fmri_local_process_installer.iss's OutputDir also points here. Cleared before each compile
# because installer filenames carry the version, so stale ones would otherwise pile up.
$InstallerPath = Join-Path $BuildOutput "installer"

# One PyInstaller onedir build per CLI; build-Installer merges them into one payload folder
# (their _internal\ runtimes come from the same .venv, so they overlap harmlessly).
$CliDir = Join-Path $PSScriptRoot "src\fmri_local_process\cli"
$Apps = [ordered]@{
    "fmri_convert_to_bids" = Join-Path $CliDir "convert_to_bids.py"
    "fmri_preprocess"      = Join-Path $CliDir "preprocess.py"
}
$AppName = "fmri_convert_to_bids"
$PackageName = "fmri-local-process"
# dcm2niix.exe + its MSVC runtime DLLs, fetched by tools\fetch_dcm2niix.ps1. Bundled into the
# app's dcm2niix\ subfolder, where processing\dcm2niix.py looks for it when frozen.
$Dcm2niixDir = Join-Path $PSScriptRoot "third_party\dcm2niix_win"

# Resolved once, up front, so `pip install -e .` and PyInstaller always run through the
# same interpreter (and so read/write the same setuptools_scm version metadata).
# Falls back to `python` on PATH if .venv doesn't exist yet.
$VenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

# $ErrorActionPreference = "Stop" does nothing for a native command's exit code -- without
# this check a failed pip/PyInstaller/ISCC call would print its error and carry on.
function Assert-Success($What) {
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed with exit code $LASTEXITCODE"
    }
}

function Show-Help {
    @"
Usage: .\build.ps1 -Full | -Exe | -Inno | -Review [-Plan <plan.json>]

  -Full   Build the exes with PyInstaller (the fmri_toolbox window with every tab, plus
          the command-line tools), assemble build_output/payload/, then compile the
          installer.

  -Exe    Only build the exes into build_output/dist/<app>/ ($(@($Apps.Keys) + 'fmri_toolbox' -join ', ')).
          No Inno Setup needed.

  -Inno   Skip the PyInstaller build. Reuses build_output/dist/<app>/
          from a previous build and (re)compiles the installer.

  -Review Build a review package: the toolbox window plus a chosen part of the data,
          for someone else to check the processing. Opens a picker (subjects, sessions,
          scans, C-PAC outputs, behavioural logs, raw DICOMs or not), builds
          fmri_toolbox.exe, then packs both into build_output/review/<name>/ (+ .zip).
          The picker remembers its last choices in build_output/review/plan.json.
          Add -Plan <file> to rebuild from a saved plan without the picker.

No option given: prints this help and exits without building anything.
"@ | Write-Host
}

function Build-Exes {
    if (-not (Test-Path (Join-Path $Dcm2niixDir "dcm2niix.exe"))) {
        & (Join-Path $PSScriptRoot "tools\fetch_dcm2niix.ps1")
    }
    $bundleArgs = Get-ChildItem $Dcm2niixDir -File | ForEach-Object {
        "--add-binary"; "$($_.FullName);dcm2niix"
    }

    & $Python -m pip install -e . --no-deps
    Assert-Success "pip install -e ."
    foreach ($app in $Apps.Keys) {
        # Only the converter needs dcm2niix; fmri_preprocess shells out to docker instead.
        $appBundleArgs = if ($app -eq $AppName) { $bundleArgs } else { @() }
        # --noconfirm: skip PyInstaller's interactive "will be REMOVED! Continue?" prompt.
        & $Python -m PyInstaller --noconfirm --workpath $WorkPath --distpath $DistPath `
            --specpath $WorkPath --name $app --onedir --console `
            --copy-metadata $PackageName @appBundleArgs $Apps[$app]
        Assert-Success "PyInstaller build of $app"
    }
    # The toolbox window with every tab -- the installer's main app. Its Setup tab converts
    # DICOMs too, so it gets dcm2niix as well.
    Build-ToolboxExe $bundleArgs
}

function Build-Installer {
    # Version from the installed package metadata -- the same source the exe was built from.
    $version = "v" + (& $Python -c "from importlib.metadata import version; print(version('$PackageName'))").Trim()
    Assert-Success "Reading package version"

    if (Test-Path $PayloadPath) { Remove-Item $PayloadPath -Recurse -Force }
    New-Item -ItemType Directory -Path $PayloadPath | Out-Null
    foreach ($app in @($Apps.Keys) + $ToolboxApp) {
        $bundleDir = Join-Path $DistPath $app
        if (-not (Test-Path $bundleDir)) {
            throw "$bundleDir not found -- run '.\build.ps1 -Full' or '-Exe' first."
        }
        # robocopy merges the shared _internal\ folders; exit codes below 8 mean success.
        robocopy $bundleDir $PayloadPath /E /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "Copying $app into the payload failed ($LASTEXITCODE)" }
    }
    # The installer's Docker setup (packaging\docker\, shipped as {app}\docker\) pulls the
    # C-PAC tag fmri_preprocess runs and builds the defacer image processing\deface.py looks for.
    Copy-Item (Join-Path $PSScriptRoot "packaging\docker") (Join-Path $PayloadPath "docker") -Recurse
    $cpacImage = (& $Python -c "from fmri_local_process.processing.cpac_docker import CPAC_IMAGE; print(CPAC_IMAGE)").Trim()
    Assert-Success "Reading CPAC_IMAGE"
    $defaceImage = (& $Python -c "from fmri_local_process.processing.deface import DEFACE_IMAGE; print(DEFACE_IMAGE)").Trim()
    Assert-Success "Reading DEFACE_IMAGE"
    $mriqcImage = (& $Python -c "from fmri_local_process.processing.mriqc import MRIQC_IMAGE; print(MRIQC_IMAGE)").Trim()
    Assert-Success "Reading MRIQC_IMAGE"

    if (Test-Path $InstallerPath) { Remove-Item $InstallerPath -Recurse -Force }

    $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $iscc)) {
        $isccCmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if (-not $isccCmd) {
            throw "ISCC.exe (Inno Setup 6 compiler) not found. Install Inno Setup 6 or add ISCC.exe to PATH."
        }
        $iscc = $isccCmd.Source
    }

    & $iscc (Join-Path $PSScriptRoot "fmri_local_process_installer.iss") "/DMyAppVersion=$version" "/DCpacImage=$cpacImage" "/DDefaceImage=$defaceImage" "/DMriqcImage=$mriqcImage"
    Assert-Success "ISCC.exe"
}

$ToolboxApp = "fmri_toolbox"
$ReviewPath = Join-Path $BuildOutput "review"

function Build-ToolboxExe($ExtraArgs = @()) {
    # The toolbox window (all tabs), windowed: no console behind it. Its splash image and
    # other package data travel with it. -Full/-Exe pass dcm2niix in $ExtraArgs; a review
    # package doesn't convert DICOMs, so it goes without. pip only refreshes the version the
    # app shows; it fails while a toolbox started from .venv is open (its exe is locked), so
    # carry on then.
    & $Python -m pip install -e . --no-deps
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "pip install -e . failed (a toolbox from .venv still open?); the app's version number may be stale."
    }
    $assets = Join-Path $PSScriptRoot "src\fmri_local_process\gui\assets"
    & $Python -m PyInstaller --noconfirm --workpath $WorkPath --distpath $DistPath `
        --specpath $WorkPath --name $ToolboxApp --onedir --windowed `
        --copy-metadata $PackageName --add-data "$assets;fmri_local_process\gui\assets" `
        @ExtraArgs (Join-Path $PSScriptRoot "packaging\fmri_toolbox.py")
    Assert-Success "PyInstaller build of $ToolboxApp"
}

function Build-Review {
    New-Item -ItemType Directory -Force -Path $ReviewPath | Out-Null
    $planFile = if ($Plan) { (Resolve-Path $Plan).Path } else { Join-Path $ReviewPath "plan.json" }
    if (-not $Plan) {
        # The picker: exit code 0 = plan saved, 2 = cancelled.
        & $Python -m fmri_local_process.gui.review_package_dialog --plan $planFile
        if ($LASTEXITCODE -eq 2) { Write-Host "Cancelled: nothing built."; return }
        Assert-Success "Review package picker"
    }
    Build-ToolboxExe
    & $Python -m fmri_local_process.cli.review_package $planFile `
        --app (Join-Path $DistPath $ToolboxApp) --out $ReviewPath
    Assert-Success "Assembling the review package"
}

if ($Review) {
    Build-Review
}
elseif ($Full) {
    Build-Exes
    Build-Installer
}
elseif ($Exe) {
    Build-Exes
}
elseif ($Inno) {
    Build-Installer
}
else {
    Show-Help
}
