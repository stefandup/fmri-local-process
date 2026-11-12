#!/usr/bin/env bash
# Per-user install of fMRI Local Process on Linux/macOS -- no sudo, no manual PATH editing.
#   app       -> ~/.local/share/fmri_local_process/
#   commands  -> launchers in ~/.local/bin/ (added to PATH in your shell rc only if it isn't
#                on PATH already -- most Linux distros already have it)
# Usage: ./install.sh            install / upgrade
#        ./install.sh --uninstall
set -euo pipefail

APP_NAME="fmri_local_process"
COMMANDS=(fmri_convert_to_bids)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME"
BIN_DIR="$HOME/.local/bin"
MARKER="# added by the $APP_NAME installer"

shell_rc() {
    case "$(basename "${SHELL:-bash}")" in
        zsh) echo "$HOME/.zshrc" ;;
        *) [[ "$(uname -s)" == Darwin ]] && echo "$HOME/.bash_profile" || echo "$HOME/.bashrc" ;;
    esac
}

remove_path_line() {
    local rc
    rc="$(shell_rc)"
    if [[ -f "$rc" ]] && grep -qF "$MARKER" "$rc"; then
        grep -vF "$MARKER" "$rc" > "$rc.tmp" && mv "$rc.tmp" "$rc"
        echo "Removed PATH entry from $rc"
    fi
}

uninstall() {
    for cmd in "${COMMANDS[@]}"; do
        rm -f "$BIN_DIR/$cmd"
    done
    rm -rf "$PREFIX"
    remove_path_line
    echo "$APP_NAME uninstalled."
}

install() {
    rm -rf "$PREFIX"
    mkdir -p "$PREFIX" "$BIN_DIR"
    cp -R "$HERE/app/." "$PREFIX/"
    if [[ "$(uname -s)" == Darwin ]]; then
        # Unsigned binaries from a download are quarantined by Gatekeeper; clear it.
        xattr -dr com.apple.quarantine "$PREFIX" 2>/dev/null || true
    fi

    # A launcher script rather than a symlink, so the bundle always runs from its own folder.
    for cmd in "${COMMANDS[@]}"; do
        printf '#!/usr/bin/env bash\nexec "%s/%s" "$@"\n' "$PREFIX" "$cmd" > "$BIN_DIR/$cmd"
        chmod +x "$BIN_DIR/$cmd" "$PREFIX/$cmd"
    done

    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        local rc
        rc="$(shell_rc)"
        if ! grep -qF "$MARKER" "$rc" 2>/dev/null; then
            printf '\nexport PATH="%s:$PATH" %s\n' "$BIN_DIR" "$MARKER" >> "$rc"
        fi
        echo "Added $BIN_DIR to PATH in $rc -- open a new terminal to use the commands."
    fi
    echo "$APP_NAME installed: ${COMMANDS[*]}"
}

case "${1:-}" in
    --uninstall) uninstall ;;
    "") install ;;
    *) echo "Usage: $0 [--uninstall]" >&2; exit 1 ;;
esac
