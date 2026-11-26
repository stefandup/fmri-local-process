#!/usr/bin/env bash
# Linux/macOS build: PyInstaller onedir bundle (with dcm2niix inside) + a .tar.gz containing
# packaging/install.sh, which installs per-user into one toolbox folder and puts that on PATH.
# Windows uses build.ps1 + Inno Setup instead. Everything lands under build_output/.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

usage() {
    cat <<USAGE
Usage: ./build.sh [--tui] [-h|--help]

Builds an installer into build_output/installer/ (a .tar.gz with install.sh inside).

  (no option)  fmri_convert_to_bids, built locally with .venv (Linux or macOS)
  --tui        fmri_tui + command-line tools, no Qt, for servers and the HPC.
               Built inside a rockylinux:8 Docker container, so it runs on
               older Linux too (glibc 2.28+). Needs Docker.
  -h, --help   show this help

Output tags:  [local] runs on this computer   [docker] runs in the container
USAGE
}

# Colour only on a terminal, and never when NO_COLOR is set.
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_LOCAL=$'\e[1;36m' C_DOCKER=$'\e[1;35m' C_OK=$'\e[1;32m' C_ERR=$'\e[1;31m' C_DIM=$'\e[2m' C_OFF=$'\e[0m'
    USE_COLOR=1
else
    C_LOCAL="" C_DOCKER="" C_OK="" C_ERR="" C_DIM="" C_OFF=""
    USE_COLOR=0
fi
START=$SECONDS
CURRENT_STEP=""

local_step() { CURRENT_STEP="$*"; echo "${C_LOCAL}[local]${C_OFF}  ==> $*"; }
docker_note() { echo "${C_DOCKER}[docker]${C_OFF} ==> $*"; }
done_msg() { echo "${C_OK}✔ $*${C_OFF} ${C_DIM}($((SECONDS - START))s)${C_OFF}"; }
trap 'echo "${C_ERR}✘ Build failed${CURRENT_STEP:+ during: $CURRENT_STEP}${C_OFF}" >&2' ERR

BUILD_IMAGE="rockylinux:8"

build_tui() {
    local installer="$PWD/build_output/installer" cache="$PWD/build_output/cache"
    mkdir -p "$installer" "$cache"
    # Only the tracked files (as they are now) and the version's git history go in: never data.
    local_step "Packing the git-tracked files to send into the container"
    local files
    files="$(mktemp)"
    trap "rm -f '$files'" EXIT
    git ls-files -z | tar --null -T - -cf "$files"

    local_step "Starting a $BUILD_IMAGE container (Docker pulls it the first time)"
    CURRENT_STEP="the Docker build (see the last [docker] step above)"
    docker run --rm \
        -v "$files:/files.tar:ro" \
        -v "$PWD/.git:/git:ro" \
        -v "$installer:/out" \
        -v "$cache:/cache" \
        -e HOST_OWNER="$(id -u):$(id -g)" \
        -e USE_COLOR="$USE_COLOR" \
        "$BUILD_IMAGE" bash -c '
            set -e
            mkdir /work && tar -xf /files.tar -C /work && cp -R /git /work/.git
            bash /work/packaging/linux/build_in_container.sh'
    CURRENT_STEP=""
    done_msg "Built $(ls "$installer"/fmri_tui-*-linux-x86_64.tar.gz)"
    echo "  Next: unpack it and run ./install.sh"
}

case "${1:-}" in
    -h|--help) usage; exit ;;
    --tui) build_tui; exit ;;
    "") ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
esac

APP_NAME="fmri_convert_to_bids"
PACKAGE_NAME="fmri-local-process"
ENTRY_SCRIPT="src/fmri_local_process/cli/convert_to_bids.py"

case "$(uname -s)" in
    Linux) PLATFORM=linux ;;
    Darwin) PLATFORM=mac ;;
    *) echo "Unsupported OS $(uname -s); use build.ps1 on Windows" >&2; exit 1 ;;
esac

BUILD_OUTPUT="$PWD/build_output"
WORK="$BUILD_OUTPUT/work/$PLATFORM"
DIST="$BUILD_OUTPUT/dist/$PLATFORM"
INSTALLER="$BUILD_OUTPUT/installer"
DCM2NIIX_DIR="$PWD/third_party/dcm2niix_$PLATFORM"

# Same interpreter for pip and PyInstaller, so the setuptools_scm version metadata agrees.
PYTHON=".venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="python3"
local_step "Building $APP_NAME for $PLATFORM with $PYTHON"

if [[ -x "$DCM2NIIX_DIR/dcm2niix" ]]; then
    local_step "dcm2niix already in third_party/dcm2niix_$PLATFORM"
else
    local_step "Downloading dcm2niix into third_party/dcm2niix_$PLATFORM"
    bash tools/fetch_dcm2niix.sh "$PLATFORM"
fi

local_step "Installing the package into the environment (pip install -e .)"
"$PYTHON" -m pip install -q -e . --no-deps

local_step "Bundling with PyInstaller (the slow step)"
"$PYTHON" -m PyInstaller \
    --noconfirm \
    --clean \
    --onedir \
    --console \
    --log-level WARN \
    --name "$APP_NAME" \
    --distpath "$DIST" \
    --workpath "$WORK" \
    --specpath "$WORK" \
    --copy-metadata "$PACKAGE_NAME" \
    --add-binary "$DCM2NIIX_DIR/dcm2niix:dcm2niix" \
    --add-data "$DCM2NIIX_DIR/license.txt:dcm2niix" \
    "$ENTRY_SCRIPT"

VERSION="$("$PYTHON" -c "from importlib.metadata import version; print(version('$PACKAGE_NAME'))")"
PACKAGE="fmri_local_process-$VERSION-$PLATFORM"
STAGE="$WORK/package/$PACKAGE"
local_step "Packaging $PACKAGE.tar.gz"
rm -rf "$WORK/package"
mkdir -p "$STAGE" "$INSTALLER"
cp -R "$DIST/$APP_NAME" "$STAGE/app"
echo "$APP_NAME" > "$STAGE/app/commands.txt"
cp packaging/install.sh "$STAGE/install.sh"
chmod +x "$STAGE/install.sh"

rm -f "$INSTALLER"/fmri_local_process-*-"$PLATFORM".tar.gz
tar -czf "$INSTALLER/$PACKAGE.tar.gz" -C "$WORK/package" "$PACKAGE"
done_msg "Built $INSTALLER/$PACKAGE.tar.gz"
echo "  Next: unpack it and run ./install.sh"
