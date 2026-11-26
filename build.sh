#!/usr/bin/env bash
# Linux/macOS build: PyInstaller onedir bundle (with dcm2niix inside) + a .tar.gz containing
# packaging/install.sh, which installs per-user into one toolbox folder and puts that on PATH.
# Windows uses build.ps1 + Inno Setup instead. Everything lands under build_output/.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

usage() {
    cat <<USAGE
Usage: ./build.sh [--tui [--remote-install TARGET]] [-h|--help]

Builds an installer into build_output/installer/ (a .tar.gz with install.sh inside).

  (no option)  fmri_convert_to_bids, built locally with .venv (Linux or macOS)
  --tui        fmri_tui + command-line tools, no Qt, for servers and the HPC.
               Built inside a rockylinux:8 Docker container, so it runs on
               older Linux too (glibc 2.28+). Needs Docker.
  --remote-install TARGET
               with --tui: after the build, install it for your user on a
               server over SSH. TARGET is what you'd give ssh: a Host from
               ~/.ssh/config (nala) or user@server. The connection is checked
               before the build. The bundle is unpacked in your home folder
               there (~/fmri_tui-<version>/, which has install.sh for
               --uninstall) and installed into ~/.local/opt/fmri_local_process.
  -h, --help   show this help

Examples:
  ./build.sh --tui
  ./build.sh --tui --remote-install nala
  ./build.sh --tui --remote-install stefan@192.0.2.10

Output tags:  [local] runs on this computer   [docker] runs in the container
              [remote] runs on the server
USAGE
}

# Colour only on a terminal, and never when NO_COLOR is set.
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_LOCAL=$'\e[1;36m' C_DOCKER=$'\e[1;35m' C_REMOTE=$'\e[1;33m' C_OK=$'\e[1;32m' C_ERR=$'\e[1;31m' C_DIM=$'\e[2m' C_OFF=$'\e[0m'
    USE_COLOR=1
else
    C_LOCAL="" C_DOCKER="" C_REMOTE="" C_OK="" C_ERR="" C_DIM="" C_OFF=""
    USE_COLOR=0
fi
START=$SECONDS
CURRENT_STEP=""

local_step() { CURRENT_STEP="$*"; echo "${C_LOCAL}[local]${C_OFF}  ==> $*"; }
remote_step() { CURRENT_STEP="$*"; echo "${C_REMOTE}[remote]${C_OFF} ==> $*"; }
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
    if [[ -z "$REMOTE" ]]; then echo "  Next: unpack it and run ./install.sh"; fi
}

check_remote() {
    remote_step "Checking the SSH connection to $REMOTE"
    ssh -o ConnectTimeout=15 "$REMOTE" true
}

install_remote() {
    # One connection: earlier unpacked versions are removed, the .tar.gz goes in on stdin, is
    # unpacked in the home folder, and installed. install.sh replaces the earlier toolbox folder;
    # the Docker images are found by name, so they stay.
    local tarball package
    tarball="$(ls build_output/installer/fmri_tui-*-linux-x86_64.tar.gz)"
    package="$(basename "$tarball" .tar.gz)"
    remote_step "Copying $(basename "$tarball") to $REMOTE:~/$package/ and installing it"
    ssh "$REMOTE" "set -e; cd
        for old in fmri_tui-*-linux-x86_64; do
            if [ -f \"\$old/install.sh\" ]; then rm -rf \"\$old\"; echo \"Removed the earlier ~/\$old\"; fi
        done
        tar -xzf -; ./'$package'/install.sh" < "$tarball"
    CURRENT_STEP=""
    done_msg "Installed on $REMOTE: log in again there (or open a new terminal) to use it"
    echo "  Uninstall there with: ~/$package/install.sh --uninstall"
}

TUI="" REMOTE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit ;;
        --tui) TUI=1 ;;
        --remote-install)
            [[ -n "${2:-}" && "$2" != -* ]] || { echo "--remote-install needs a TARGET, e.g. nala or user@server" >&2; exit 1; }
            REMOTE="$2"
            shift ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
    shift
done
if [[ -n "$REMOTE" && -z "$TUI" ]]; then
    echo "--remote-install only works with --tui (the server bundle)" >&2
    exit 1
fi
if [[ -n "$TUI" ]]; then
    if [[ -n "$REMOTE" ]]; then check_remote; fi
    build_tui
    if [[ -n "$REMOTE" ]]; then install_remote; fi
    exit
fi

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
