#!/usr/bin/env bash
# Builds the Linux bundle inside a rockylinux:8 container (build.sh --tui starts it): an old glibc
# (2.28), so the result runs on RHEL/Rocky 8 and later, Ubuntu 20.04 and later, Debian 10 and later.
#   /work   a copy of the tracked files     /out    the bundle goes here     /cache  downloads
set -euo pipefail

echo "== Tools"
dnf install -y -q --enablerepo=powertools python3.12 python3.12-pip binutils git gcc-c++ \
    libstdc++-static make cmake tar gzip findutils >/dev/null
export PIP_CACHE_DIR=/cache/pip PIP_DISABLE_PIP_VERSION_CHECK=1

echo "== dcm2niix $(cat /work/tools/dcm2niix_version.txt)"
# From source: the release's own binary needs a newer glibc (2.29) than RHEL/Rocky 8 has.
DCM2NIIX_TAG="$(head -n 1 /work/tools/dcm2niix_version.txt | tr -d '[:space:]')"
export DCM2NIIX_DIR="/cache/dcm2niix-$DCM2NIIX_TAG-el8"
if [[ ! -x "$DCM2NIIX_DIR/dcm2niix" ]]; then
    git clone -q --depth 1 -b "$DCM2NIIX_TAG" https://github.com/rordenlab/dcm2niix /tmp/dcm2niix
    cmake -S /tmp/dcm2niix -B /tmp/dcm2niix/build -DCMAKE_BUILD_TYPE=Release \
        -DUSE_OPENJPEG=ON -DUSE_JPEGLS=ON > /tmp/cmake.log 2>&1 \
        && make -C /tmp/dcm2niix/build -j"$(nproc)" >> /tmp/cmake.log 2>&1 \
        || { tail -n 40 /tmp/cmake.log; exit 1; }
    mkdir -p "$DCM2NIIX_DIR"
    cp /tmp/dcm2niix/license.txt "$DCM2NIIX_DIR/"
    cp /tmp/dcm2niix/build/bin/dcm2niix "$DCM2NIIX_DIR/"
fi
"$DCM2NIIX_DIR/dcm2niix" -v | head -n 1 || true

echo "== Python and packages"
python3.12 -m venv /opt/venv
/opt/venv/bin/pip install -q --upgrade pip
/opt/venv/bin/pip install -q pyinstaller /work

echo "== Bundle"
cd /work
/opt/venv/bin/python -m PyInstaller --noconfirm --log-level WARN \
    --distpath /tmp/dist --workpath /tmp/build specs/linux.spec
if find /tmp/dist -iname "*pyside*" -o -iname "libQt*" | grep -q .; then
    echo "Qt got into the bundle" >&2
    exit 1
fi

VERSION="$(/opt/venv/bin/python -c "from importlib.metadata import version; print(version('fmri-local-process'))")"
PACKAGE="fmri_tui-$VERSION-linux-x86_64"
STAGE="/tmp/stage/$PACKAGE"
mkdir -p "$STAGE"
cp -R /tmp/dist/fmri_tui "$STAGE/app"
(cd "$STAGE/app" && find . -maxdepth 1 -type f -perm -u+x -printf '%P\n' | sort) > "$STAGE/app/commands.txt"
cp packaging/install.sh "$STAGE/install.sh"
mkdir "$STAGE/docker"  # the C-PAC / MRIQC / defacer images, as in the Windows installer
cp packaging/docker/Dockerfile packaging/docker/setup_docker_image.sh "$STAGE/docker/"
chmod +x "$STAGE/docker/setup_docker_image.sh"
chmod +x "$STAGE/install.sh"
rm -f /out/fmri_tui-*-linux-x86_64.tar.gz
tar -czf "/out/$PACKAGE.tar.gz" -C /tmp/stage "$PACKAGE"
chown -R "$HOST_OWNER" /out /cache
echo "Commands: $(tr '\n' ' ' < "$STAGE/app/commands.txt")"
