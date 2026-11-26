#!/usr/bin/env bash
# Installs fMRI Local Process for you on Linux/macOS, without sudo: every tool goes into one toolbox
# folder, and that folder is added to your PATH (as the Windows installer does).
# Usage: ./install.sh [FOLDER]                install / upgrade
#        ./install.sh --uninstall [FOLDER]
# FOLDER: the toolbox folder, by default ~/.local/opt/fmri_local_process
set -euo pipefail

APP_NAME="fmri_local_process"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMANDS_FILE="commands.txt"  # in the toolbox folder: one command per line
MARKER="# added by the $APP_NAME installer"
# Before the toolbox folder was on PATH itself: the app here, launchers in ~/.local/bin.
OLD_PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME"
OLD_BIN_DIR="$HOME/.local/bin"

startup_files() {
    # Terminals read the first, logins (ssh, tmux) the others; .bash_profile hides .profile.
    if [[ "$(basename "${SHELL:-bash}")" == zsh ]]; then
        echo "$HOME/.zshrc"
        return
    fi
    echo "$HOME/.bashrc"
    echo "$HOME/.profile"
    local file
    for file in "$HOME/.bash_profile" "$HOME/.bash_login"; do
        if [[ -f "$file" ]]; then echo "$file"; fi
    done
}

remove_from_path() {
    local file kept
    for file in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile" "$HOME/.bash_profile" "$HOME/.bash_login"; do
        if [[ -f "$file" ]] && grep -qF "$MARKER" "$file"; then
            kept="$(grep -vF "$MARKER" "$file" || true)"
            if [[ -n "$kept" ]]; then printf '%s\n' "$kept" > "$file"; else rm -f "$file"; fi
        fi
    done
}

add_to_path() {
    local quoted file
    quoted="$(printf '%s' "$1" | sed 's/[\\"$`]/\\&/g')"
    while IFS= read -r file; do
        printf 'case ":$PATH:" in *":%s:"*) ;; *) export PATH="$PATH:%s" ;; esac %s\n' \
            "$quoted" "$quoted" "$MARKER" >> "$file"
    done < <(startup_files)
}

remove_toolbox() {
    # Only ever a toolbox folder: never one that holds something else.
    local folder="$1"
    [[ -e "$folder" ]] || return 0
    if [[ -f "$folder/$COMMANDS_FILE" && -d "$folder/_internal" ]]; then
        rm -rf "$folder"
    elif ! rmdir "$folder" 2>/dev/null; then
        echo "$folder holds something else: choose another folder." >&2
        exit 1
    fi
}

remove_old_install() {
    [[ -d "$OLD_PREFIX/_internal" && "$OLD_PREFIX" != "$FOLDER" ]] || return 0
    local launcher
    for launcher in "$OLD_BIN_DIR"/fmri_*; do
        if [[ -f "$launcher" && ! -L "$launcher" ]] && grep -qF "exec \"$OLD_PREFIX/" "$launcher"; then
            rm -f "$launcher"
        fi
    done
    rm -rf "$OLD_PREFIX"
}

install() {
    remove_toolbox "$FOLDER"
    remove_old_install
    mkdir -p "$FOLDER"
    cp -R "$HERE/app/." "$FOLDER/"
    if [[ -d "$HERE/docker" ]]; then cp -R "$HERE/docker" "$FOLDER/docker"; fi
    # A copy of this script, for --uninstall without the unpacked bundle (fmri_update uses it).
    mkdir -p "$FOLDER/.installer"
    cp "$HERE/install.sh" "$FOLDER/.installer/install.sh"
    if [[ "$(uname -s)" == Darwin ]]; then
        # Unsigned binaries from a download are quarantined by Gatekeeper; clear it.
        xattr -dr com.apple.quarantine "$FOLDER" 2>/dev/null || true
    fi
    remove_from_path
    add_to_path "$FOLDER"
    echo "Installed in $FOLDER, now on your PATH: open a new terminal (or log in again) to use"
    sed 's/^/  /' "$FOLDER/$COMMANDS_FILE"
}

ACTION=install
if [[ "${1:-}" == --uninstall ]]; then
    ACTION=uninstall
    shift
fi
if [[ $# -gt 1 || "${1:-}" == -* ]]; then
    echo "Usage: $0 [--uninstall] [FOLDER]" >&2
    exit 1
fi
FOLDER="${1:-$HOME/.local/opt/$APP_NAME}"
if [[ "$FOLDER" != /* ]]; then FOLDER="$PWD/$FOLDER"; fi
FOLDER="${FOLDER%/}"
if [[ "$FOLDER" == *:* ]]; then
    echo "A folder on PATH can't have ':' in its name." >&2
    exit 1
fi

if [[ "$ACTION" == install ]]; then
    install
else
    remove_toolbox "$FOLDER"
    remove_old_install
    remove_from_path
    echo "$APP_NAME uninstalled. Settings and backups are kept."
fi
