#!/usr/bin/env bash
# Linux/macOS build: PyInstaller onedir bundle (with dcm2niix inside) + a .tar.gz containing
# packaging/install.sh, which installs per-user and puts the commands on PATH.
# Windows uses build.ps1 + Inno Setup instead. Everything lands under build_output/.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

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

[[ -x "$DCM2NIIX_DIR/dcm2niix" ]] || bash tools/fetch_dcm2niix.sh "$PLATFORM"

"$PYTHON" -m pip install -e . --no-deps
"$PYTHON" -m PyInstaller \
    --noconfirm \
    --clean \
    --onedir \
    --console \
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
rm -rf "$WORK/package"
mkdir -p "$STAGE" "$INSTALLER"
cp -R "$DIST/$APP_NAME" "$STAGE/app"
cp packaging/install.sh "$STAGE/install.sh"
chmod +x "$STAGE/install.sh"

rm -f "$INSTALLER"/fmri_local_process-*-"$PLATFORM".tar.gz
tar -czf "$INSTALLER/$PACKAGE.tar.gz" -C "$WORK/package" "$PACKAGE"
echo "Built $INSTALLER/$PACKAGE.tar.gz -- unpack it and run ./install.sh"
