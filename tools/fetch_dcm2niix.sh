#!/usr/bin/env bash
# Downloads the Linux or macOS dcm2niix release build into third_party/dcm2niix_<platform>/
# (gitignored). Linux/macOS counterpart of fetch_dcm2niix.ps1; the release tag is pinned in
# tools/dcm2niix_version.txt.
# Usage: tools/fetch_dcm2niix.sh [linux|mac]   (default: this machine's platform)
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="$(head -n 1 "$TOOLS_DIR/dcm2niix_version.txt" | tr -d '[:space:]')"
REPO="rordenlab/dcm2niix"

PLATFORM="${1:-}"
if [[ -z "$PLATFORM" ]]; then
    case "$(uname -s)" in
        Linux) PLATFORM=linux ;;
        Darwin) PLATFORM=mac ;;
        *) echo "Unsupported OS $(uname -s); use fetch_dcm2niix.ps1 on Windows" >&2; exit 1 ;;
    esac
fi
case "$PLATFORM" in
    linux) ASSET=dcm2niix_lnx.zip ;;
    mac) ASSET=dcm2niix_mac.zip ;;
    *) echo "Platform must be linux or mac, got '$PLATFORM'" >&2; exit 1 ;;
esac

DEST="$TOOLS_DIR/../third_party/dcm2niix_$PLATFORM"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading dcm2niix $TAG ($ASSET)"
curl -fsSL -o "$TMP/$ASSET" "https://github.com/$REPO/releases/download/$TAG/$ASSET"
rm -rf "$DEST"
mkdir -p "$DEST"
# python's zipfile is always there (unzip often isn't); it drops the exec bit, so restore it.
python3 -m zipfile -e "$TMP/$ASSET" "$DEST"
chmod +x "$DEST/dcm2niix"
curl -fsSL -o "$DEST/license.txt" "https://raw.githubusercontent.com/$REPO/$TAG/license.txt"
printf '%s' "$TAG" > "$DEST/VERSION"

HOST="$(uname -s)"
if [[ ($PLATFORM == linux && $HOST == Linux) || ($PLATFORM == mac && $HOST == Darwin) ]]; then
    "$DEST/dcm2niix" -v | head -n 1 || true
fi
echo "dcm2niix $TAG installed at $DEST/dcm2niix"
