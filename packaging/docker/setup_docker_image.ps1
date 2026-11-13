<#
.SYNOPSIS
Download C-PAC and build the local philani_fmri_process Docker image with a T1w defacer.

.DESCRIPTION
Run by the installer (in a visible window, so Docker's per-layer download progress shows),
and safe to re-run by hand, e.g. to switch defacer:

  powershell -ExecutionPolicy Bypass -File setup_docker_image.ps1 -Defacer pydeface

-Defacer freesurfer  mri_deface from the official FreeSurfer image (extra ~5 GB download,
                     removed again after the build; only mri_deface + 2 templates are kept)
-Defacer local       mri_deface + templates from -DefaceFiles (a FreeSurfer home, or a folder
                     holding mri_deface, talairach_mixed_with_skull.gca and face.gca)
-Defacer pydeface    pydeface, installed with pip (small)
-Defacer none        only download C-PAC (fmri_preprocess); no defacer image is built

Written for Windows PowerShell 5.1, which every target PC has.
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("freesurfer", "local", "pydeface", "none")]
    [string]$Defacer,
    [string]$DefaceFiles = "",
    # Defaults match processing/cpac_docker.py CPAC_IMAGE and processing/deface.py DEFACE_IMAGE;
    # the installer passes the values build.ps1 read from those modules.
    [string]$CpacImage = "fcpindi/c-pac:release-v1.8.7",
    [string]$Image = "philani_fmri_process:latest",
    [string]$FreesurferImage = "freesurfer/freesurfer:7.1.1",
    # Wait for Enter before closing on failure, so the window's error stays readable.
    [switch]$PauseOnError
)

# Not "Stop": Windows PowerShell 5.1 then aborts on any docker stderr line (its progress and
# warnings go there). Every docker call checks $LASTEXITCODE; file copies use -ErrorAction Stop.
$ErrorActionPreference = "Continue"
$Activity = "Setting up Docker for fMRI Local Process"
$Templates = @("talairach_mixed_with_skull.gca", "face.gca")

function Fail([string]$Message) {
    Write-Progress -Activity $Activity -Completed
    Write-Host ""
    Write-Host "FAILED: $Message" -ForegroundColor Red
    if ($PauseOnError) { Read-Host "Press Enter to close this window" | Out-Null }
    exit 1
}

function Step([int]$Number, [int]$Total, [string]$What) {
    Write-Progress -Activity $Activity -Status "Step $Number of ${Total}: $What" `
        -PercentComplete ([int](100 * ($Number - 1) / $Total))
    Write-Host ""
    Write-Host "== Step $Number of ${Total}: $What" -ForegroundColor Cyan
}

# Docker Hub downloads of 10+ GB sometimes drop ('unexpected EOF'); finished layers are
# kept, so retrying resumes rather than restarts.
function Get-DockerImage([string]$Name) {
    if (docker image ls -q $Name) {
        Write-Host "$Name is already downloaded."
        return
    }
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        docker pull $Name
        if ($LASTEXITCODE -eq 0) { return }
        Write-Host "Download interrupted (attempt $attempt of 3); retrying..." -ForegroundColor Yellow
    }
    Fail "docker pull $Name failed 3 times. Check the internet connection and re-run this script."
}

# mri_deface + templates: FreeSurfer-home layout (bin/, average/) or one flat folder.
function Find-DefaceFile([string]$Folder, [string]$Name, [string]$SubFolder) {
    foreach ($candidate in @((Join-Path (Join-Path $Folder $SubFolder) $Name), (Join-Path $Folder $Name))) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    Fail "$Name not found in $Folder (nor in its $SubFolder\ subfolder)."
}

docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop is not running. Start it, wait for 'Engine running', and re-run." }

$total = if ($Defacer -eq "none") { 1 } elseif ($Defacer -eq "freesurfer") { 4 } else { 2 }

Step 1 $total "Download C-PAC ($CpacImage, ~15 GB unpacked; can take an hour or more)"
Get-DockerImage $CpacImage
if ($Defacer -eq "none") {
    Write-Progress -Activity $Activity -Completed
    Write-Host "Done. No defacer image built: T1w images can only be defaced with --freesurfer, or not at all (--no-deface)." -ForegroundColor Green
    exit 0
}

$step = 2
if ($Defacer -eq "freesurfer") {
    Step $step $total "Download FreeSurfer ($FreesurferImage, ~5 GB; only mri_deface is kept)"
    Get-DockerImage $FreesurferImage
    $step++
}

# A fresh build context: the Dockerfile, plus deface_files/ (empty unless -Defacer local).
$context = Join-Path ([System.IO.Path]::GetTempPath()) ("philani_docker_" + [guid]::NewGuid().ToString("N"))
$filesDir = Join-Path $context "deface_files"
New-Item -ItemType Directory -Path $filesDir -Force -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Dockerfile") -Destination $context -ErrorAction Stop
if ($Defacer -eq "local") {
    if (-not $DefaceFiles -or -not (Test-Path -LiteralPath $DefaceFiles -PathType Container)) {
        Fail "-Defacer local needs -DefaceFiles <folder>; got '$DefaceFiles'."
    }
    Copy-Item -LiteralPath (Find-DefaceFile $DefaceFiles "mri_deface" "bin") -Destination $filesDir -ErrorAction Stop
    foreach ($template in $Templates) {
        Copy-Item -LiteralPath (Find-DefaceFile $DefaceFiles $template "average") -Destination $filesDir -ErrorAction Stop
    }
}

Step $step $total "Build $Image with the $Defacer defacer"
$env:DOCKER_BUILDKIT = "1"
docker build --build-arg "DEFACER=$Defacer" --build-arg "CPAC_IMAGE=$CpacImage" `
    --build-arg "FREESURFER_IMAGE=$FreesurferImage" -t $Image $context
$buildExit = $LASTEXITCODE
Remove-Item -LiteralPath $context -Recurse -Force -ErrorAction SilentlyContinue
if ($buildExit -ne 0) { Fail "docker build failed (exit $buildExit); see the output above." }
$step++

if ($Defacer -eq "freesurfer") {
    Step $step $total "Remove the full FreeSurfer image again (frees ~10 GB)"
    docker image rm $FreesurferImage | Out-Null
}

Write-Progress -Activity $Activity -Completed
Write-Host ""
Write-Host "Done: $Image is ready (defacer: $Defacer)." -ForegroundColor Green
exit 0
