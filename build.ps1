#Requires -Version 7
param(
    [switch]$Full,
    [switch]$Exe,
    [switch]$Inno
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
Usage: .\build.ps1 -Full | -Exe | -Inno

  -Full   Build the exe with PyInstaller, assemble build_output/payload/,
          then compile the installer.

  -Exe    Only build the exes into build_output/dist/<app>/ ($($Apps.Keys -join ', ')).
          No Inno Setup needed.

  -Inno   Skip the PyInstaller build. Reuses build_output/dist/<app>/
          from a previous build and (re)compiles the installer.

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
}

function Build-Installer {
    # Version from the installed package metadata -- the same source the exe was built from.
    $version = "v" + (& $Python -c "from importlib.metadata import version; print(version('$PackageName'))").Trim()
    Assert-Success "Reading package version"

    if (Test-Path $PayloadPath) { Remove-Item $PayloadPath -Recurse -Force }
    New-Item -ItemType Directory -Path $PayloadPath | Out-Null
    foreach ($app in $Apps.Keys) {
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

    if (Test-Path $InstallerPath) { Remove-Item $InstallerPath -Recurse -Force }

    $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $iscc)) {
        $isccCmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if (-not $isccCmd) {
            throw "ISCC.exe (Inno Setup 6 compiler) not found. Install Inno Setup 6 or add ISCC.exe to PATH."
        }
        $iscc = $isccCmd.Source
    }

    & $iscc (Join-Path $PSScriptRoot "fmri_local_process_installer.iss") "/DMyAppVersion=$version" "/DCpacImage=$cpacImage" "/DDefaceImage=$defaceImage"
    Assert-Success "ISCC.exe"
}

if ($Full) {
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
