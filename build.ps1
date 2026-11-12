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

# PLACEHOLDER: the entry script and the exe/bundle name. The entry script doesn't exist yet.
$EntryScript = Join-Path $PSScriptRoot "src\fmri_local_process\main.py"
$AppName = "fmri_local_process"
$PackageName = "fmri-local-process"

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

  -Exe    Only build the exe into build_output/dist/$AppName/.
          No Inno Setup needed.

  -Inno   Skip the PyInstaller build. Reuses build_output/dist/$AppName/
          from a previous build and (re)compiles the installer.

No option given: prints this help and exits without building anything.
"@ | Write-Host
}

function Build-Exes {
    & $Python -m pip install -e . --no-deps
    Assert-Success "pip install -e ."
    # --noconfirm: skip PyInstaller's interactive "will be REMOVED! Continue?" prompt.
    & $Python -m PyInstaller --noconfirm --workpath $WorkPath --distpath $DistPath `
        --specpath $WorkPath --name $AppName --onedir $EntryScript
    Assert-Success "PyInstaller build"
}

function Build-Installer {
    # Version from the installed package metadata -- the same source the exe was built from.
    $version = "v" + (& $Python -c "from importlib.metadata import version; print(version('$PackageName'))").Trim()
    Assert-Success "Reading package version"

    $bundleDir = Join-Path $DistPath $AppName
    if (-not (Test-Path $bundleDir)) {
        throw "$bundleDir not found -- run '.\build.ps1 -Full' or '-Exe' first."
    }

    if (Test-Path $PayloadPath) { Remove-Item $PayloadPath -Recurse -Force }
    New-Item -ItemType Directory -Path $PayloadPath | Out-Null
    Copy-Item (Join-Path $bundleDir "*") $PayloadPath -Recurse

    if (Test-Path $InstallerPath) { Remove-Item $InstallerPath -Recurse -Force }

    $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $iscc)) {
        $isccCmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if (-not $isccCmd) {
            throw "ISCC.exe (Inno Setup 6 compiler) not found. Install Inno Setup 6 or add ISCC.exe to PATH."
        }
        $iscc = $isccCmd.Source
    }

    & $iscc (Join-Path $PSScriptRoot "fmri_local_process_installer.iss") "/DMyAppVersion=$version"
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
