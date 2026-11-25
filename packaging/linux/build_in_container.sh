#!/usr/bin/env bash
# Builds the Linux bundle inside a rockylinux:8 container (build.sh --tui starts it): an old glibc
# (2.28), so the result runs on RHEL/Rocky 8 and later, Ubuntu 20.04 and later, Debian 10 and later.
#   /work   a copy of the tracked files     /out    the bundle goes here     /cache  downloads
set -euo pipefail

UV_VERSION="0.13.10"
UV_SHA256="bf65ac1a0af10f92c522d91ac096c7c0a39657647b155577e7836c2cbbb4526c"
PYTHON_VERSION="3.13"
export UV_CACHE_DIR=/cache/uv UV_PYTHON_INSTALL_DIR=/opt/python UV_LINK_MODE=copy

echo "== Tools"
dnf install -y -q --enablerepo=powertools binutils git gcc-c++ libstdc++-static make cmake tar gzip findutils \
    >/dev/null

UV_TAR="/cache/uv-$UV_VERSION.tar.gz"
[[ -f "$UV_TAR" ]] || curl -fsSL -o "$UV_TAR" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz"
echo "$UV_SHA256  $UV_TAR" | sha256sum -c --quiet
tar -xzf "$UV_TAR" -C /usr/local/bin --strip-components=1

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
uv venv -q --python "$PYTHON_VERSION" /opt/venv
uv pip install -q --python /opt/venv/bin/python pyinstaller "/work[tui]"

echo "== Bundle"
cd /work
/opt/venv/bin/python -m PyInstaller --noconfirm --log-level WARN \
    --distpath /tmp/dist --workpath /tmp/build packaging/linux/fmri_tui.spec
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
chmod +x "$STAGE/install.sh"
rm -f /out/fmri_tui-*-linux-x86_64.tar.gz
tar -czf "/out/$PACKAGE.tar.gz" -C /tmp/stage "$PACKAGE"
chown -R "$HOST_OWNER" /out /cache
echo "Commands: $(tr '\n' ' ' < "$STAGE/app/commands.txt")"
