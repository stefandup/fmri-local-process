#Requires -Version 7
# Downloads the Windows dcm2niix release build into third_party/dcm2niix_win/ (gitignored).
# Upstream: https://github.com/rordenlab/dcm2niix -- we only wrap its exe, not the source.
# The release tag is pinned in tools/dcm2niix_version.txt (shared with fetch_dcm2niix.sh);
# bump it there to upgrade.
param(
    [string]$Tag = (Get-Content (Join-Path $PSScriptRoot "dcm2niix_version.txt") -TotalCount 1).Trim()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Repo = "rordenlab/dcm2niix"
$Dest = (New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot "..\third_party\dcm2niix_win")).FullName

$ZipUrl = "https://github.com/$Repo/releases/download/$Tag/dcm2niix_win.zip"
$LicenseUrl = "https://raw.githubusercontent.com/$Repo/$Tag/license.txt"
$Zip = Join-Path ([System.IO.Path]::GetTempPath()) "dcm2niix_win_$Tag.zip"

Write-Host "Downloading dcm2niix $Tag from $ZipUrl"
Invoke-WebRequest -Uri $ZipUrl -OutFile $Zip
Get-ChildItem $Dest -File | Remove-Item -Force
Expand-Archive -Path $Zip -DestinationPath $Dest -Force
Remove-Item $Zip -Force
Invoke-WebRequest -Uri $LicenseUrl -OutFile (Join-Path $Dest "license.txt")
Set-Content -Path (Join-Path $Dest "VERSION") -Value $Tag -NoNewline

$Exe = Join-Path $Dest "dcm2niix.exe"
if (-not (Test-Path $Exe)) {
    throw "dcm2niix.exe not found in the release zip -- check the asset layout for $Tag."
}
& $Exe -v | Select-Object -First 1
Write-Host "dcm2niix $Tag installed at $Exe"
