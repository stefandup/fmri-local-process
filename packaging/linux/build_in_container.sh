#!/usr/bin/env bash
# Builds the Linux bundle inside a rockylinux:8 container (build.sh --tui starts it): an old glibc
# (2.28), so the result runs on RHEL/Rocky 8 and later, Ubuntu 20.04 and later, Debian 10 and later.
#   /work   a copy of the tracked files     /out    the bundle goes here     /cache  downloads
set -Eeuo pipefail

# build.sh passes USE_COLOR=1 when its own output is a terminal (the container's isn't).
if [[ "${USE_COLOR:-0}" == 1 ]]; then
    C_DOCKER=$'\e[1;35m' C_ERR=$'\e[1;31m' C_DIM=$'\e[2m' C_OFF=$'\e[0m'
else
    C_DOCKER="" C_ERR="" C_DIM="" C_OFF=""
fi
START=$SECONDS
STEP=""
step() { STEP="$*"; echo "${C_DOCKER}[docker]${C_OFF} ==> $* ${C_DIM}(+$((SECONDS - START))s)${C_OFF}"; }
info() { echo "${C_DOCKER}[docker]${C_OFF}     $*"; }
trap 'echo "${C_ERR}[docker] ✘ failed during: $STEP${C_OFF}" >&2' ERR

step "Installing build tools with dnf (Python 3.12, gcc, cmake, git)"
dnf install -y -q --enablerepo=powertools python3.12 python3.12-pip binutils git gcc-c++ \
    libstdc++-static make cmake tar gzip findutils >/dev/null
export PIP_CACHE_DIR=/cache/pip PIP_DISABLE_PIP_VERSION_CHECK=1

step "dcm2niix $(head -n 1 /work/tools/dcm2niix_version.txt)"
# From source: the release's own binary needs a newer glibc (2.29) than RHEL/Rocky 8 has.
DCM2NIIX_TAG="$(head -n 1 /work/tools/dcm2niix_version.txt | tr -d '[:space:]')"
export DCM2NIIX_DIR="/cache/dcm2niix-$DCM2NIIX_TAG-el8"
if [[ -x "$DCM2NIIX_DIR/dcm2niix" ]]; then
    info "using the cached build in build_output/cache"
else
    info "compiling from source (only the first time; cached afterwards)"
    git clone -q --depth 1 -b "$DCM2NIIX_TAG" https://github.com/rordenlab/dcm2niix /tmp/dcm2niix
    cmake -S /tmp/dcm2niix -B /tmp/dcm2niix/build -DCMAKE_BUILD_TYPE=Release \
        -DUSE_OPENJPEG=ON -DUSE_JPEGLS=ON > /tmp/cmake.log 2>&1 \
        && make -C /tmp/dcm2niix/build -j"$(nproc)" >> /tmp/cmake.log 2>&1 \
        || { tail -n 40 /tmp/cmake.log; exit 1; }
    mkdir -p "$DCM2NIIX_DIR"
    cp /tmp/dcm2niix/license.txt "$DCM2NIIX_DIR/"
    cp /tmp/dcm2niix/build/bin/dcm2niix "$DCM2NIIX_DIR/"
fi
info "$("$DCM2NIIX_DIR/dcm2niix" -v 2>&1 | head -n 1 || true)"

step "Creating a Python 3.12 venv and installing pyinstaller + the package"
python3.12 -m venv /opt/venv
/opt/venv/bin/pip install -q --upgrade pip
/opt/venv/bin/pip install -q pyinstaller /work

step "Bundling with PyInstaller (the slow step)"
cd /work
/opt/venv/bin/python -m PyInstaller --noconfirm --log-level WARN \
    --distpath /tmp/dist --workpath /tmp/build specs/linux.spec
step "Checking that no Qt got into the bundle"
if find /tmp/dist -iname "*pyside*" -o -iname "libQt*" | grep -q .; then
    echo "${C_ERR}[docker] Qt got into the bundle${C_OFF}" >&2
    exit 1
fi

VERSION="$(/opt/venv/bin/python -c "from importlib.metadata import version; print(version('fmri-local-process'))")"
PACKAGE="fmri_tui-$VERSION-linux-x86_64"
STAGE="/tmp/stage/$PACKAGE"
step "Packaging $PACKAGE.tar.gz"
mkdir -p "$STAGE"
cp -R /tmp/dist/fmri_tui "$STAGE/app"
cp packaging/linux/fmri_update "$STAGE/app/"  # the updater, a command like the others
chmod +x "$STAGE/app/fmri_update"
echo "$VERSION" > "$STAGE/app/VERSION"  # what fmri_update compares with the latest release
(cd "$STAGE/app" && find . -maxdepth 1 -type f -perm -u+x -printf '%P\n' | sort) > "$STAGE/app/commands.txt"
cp packaging/install.sh "$STAGE/install.sh"
mkdir "$STAGE/docker"  # the C-PAC / MRIQC / defacer images, as in the Windows installer
cp packaging/docker/Dockerfile packaging/docker/setup_docker_image.sh "$STAGE/docker/"
chmod +x "$STAGE/docker/setup_docker_image.sh"
chmod +x "$STAGE/install.sh"
rm -f /out/fmri_tui-*-linux-x86_64.tar.gz
tar -czf "/out/$PACKAGE.tar.gz" -C /tmp/stage "$PACKAGE"
chown -R "$HOST_OWNER" /out /cache
info "Commands: $(tr '\n' ' ' < "$STAGE/app/commands.txt")"
