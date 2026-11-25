#!/usr/bin/env bash
# Installs the newest Linux bundle in clean containers, as an ordinary user, and starts everything in
# it: the command-line tools, the bundled dcm2niix and, in a terminal (tmux), the terminal app.
# Usage: packaging/linux/check_bundle.sh [image ...]       (needs Docker and tmux)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

IMAGES=("$@")
(( ${#IMAGES[@]} )) || IMAGES=(ubuntu:22.04 rockylinux:8 debian:12)
TARBALL="$(ls -t build_output/installer/fmri_tui-*-linux-x86_64.tar.gz | head -n 1)"
SESSION="fmri_check_$$"
trap 'tmux kill-session -t "$SESSION" 2>/dev/null || true; docker rm -f "$SESSION" >/dev/null 2>&1 || true' EXIT

in_container() { docker exec -u tester -e HOME=/home/tester "$@"; }

check_tui() {
    # $@: extra docker exec options, e.g. the locale
    tmux new-session -d -s "$SESSION" -x 120 -y 40 \
        "docker exec -it -u tester -e HOME=/home/tester -e TERM=xterm-256color $* $SESSION /home/tester/.local/bin/fmri_tui"
    local screen="" i
    for i in $(seq 1 60); do
        sleep 0.5
        screen="$(tmux capture-pane -p -t "$SESSION" 2>/dev/null || true)"
        grep -q "1. Setup & Convert to BIDS" <<<"$screen" && break
    done
    grep -q "1. Setup & Convert to BIDS" <<<"$screen" || { echo "$screen"; return 1; }
    tmux send-keys -t "$SESSION" 0  # the last tab loads the heaviest parts
    sleep 2
    tmux capture-pane -p -t "$SESSION" | grep -q "Run first-level stats" || return 1
    tmux send-keys -t "$SESSION" C-q
    for i in $(seq 1 20); do
        sleep 0.5
        tmux has-session -t "$SESSION" 2>/dev/null || return 0
    done
    echo "didn't quit" >&2
    return 1
}

failed=0
for image in "${IMAGES[@]}"; do
    echo "== $image"
    docker rm -f "$SESSION" >/dev/null 2>&1 || true
    docker run -d --rm --name "$SESSION" --network none -v "$PWD/$TARBALL:/bundle.tar.gz:ro" \
        "$image" sleep 1800 >/dev/null
    docker exec "$SESSION" useradd -m tester
    if in_container "$SESSION" bash -ec '
            mkdir -p ~/unpack && tar -xzf /bundle.tar.gz -C ~/unpack
            ~/unpack/*/install.sh
            export PATH="$HOME/.local/bin:$PATH" LC_ALL=C
            for command in $(cat ~/.local/share/fmri_local_process/commands.txt); do
                $command --help >/dev/null
                echo "  ok  $command"
            done
            fmri_tui --version
            ~/.local/share/fmri_local_process/_internal/dcm2niix/dcm2niix -v | head -n 1
            fmri_tui </dev/null 2>&1 | grep -q "needs a terminal"
            # Crosschecking by commands alone: a typo in a made-up log is found, fixed, and matches.
            fmri_generate_sample_data ~/dummy --n-clean 1 --with-errors --seed 1 >/dev/null
            fmri_folders --behav ~/dummy/behav --dicom ~/dummy/dicom --bids ~/bids --yes >/dev/null 2>&1
            before="$(fmri_behav_check 2>/dev/null | tail -n 1)"
            fmri_behav_fix log DUMMY0033-emo --id DUMMY003 --crosschecked >/dev/null 2>&1
            after="$(fmri_behav_check 2>/dev/null | tail -n 1)"
            echo "  $before  ->  $after"
            test "$before" != "$after"
            fmri_behav_check --log DUMMY0033-emo 2>/dev/null | grep -q "matched -> sub-DUMMY003_ses-1"' \
        && check_tui && echo "  ok  terminal app" \
        && check_tui -e LC_ALL=C && echo "  ok  terminal app, C locale" \
        && in_container "$SESSION" bash -ec '
            ~/unpack/*/install.sh --uninstall
            test ! -e ~/.local/share/fmri_local_process && test -z "$(ls ~/.local/bin)"'; then
        echo "  $image: PASSED"
    else
        echo "  $image: FAILED"
        failed=1
    fi
done
exit $failed
