#!/usr/bin/env bash
# Downloads C-PAC (and MRIQC) and builds the local philani_fmri_process image with a T1w defacer: the
# Linux counterpart of setup_docker_image.ps1. Safe to run again, e.g. to switch defacer.
#
#   ./setup_docker_image.sh --defacer freesurfer   mri_deface from the official FreeSurfer image (extra
#                                                  ~5 GB download, removed again after the build)
#   ./setup_docker_image.sh --defacer pydeface     pydeface, installed with pip (small)
#   ./setup_docker_image.sh --defacer local --deface-files <folder>
#                                                  your own mri_deface and templates (a FreeSurfer
#                                                  home, or one folder with all three files)
#   ./setup_docker_image.sh --defacer none         only download
#   --mriqc     also download MRIQC (Scan quality, ~5 GB)
#   --no-cpac   skip the C-PAC download (only with --defacer none: a defacer is built on it)
set -euo pipefail

# As processing/cpac_docker.py CPAC_IMAGE, deface.py DEFACE_IMAGE and mriqc.py MRIQC_IMAGE.
CPAC_IMAGE="${CPAC_IMAGE:-fcpindi/c-pac:release-v1.8.7}"
IMAGE="${IMAGE:-philani_fmri_process:latest}"
FREESURFER_IMAGE="${FREESURFER_IMAGE:-freesurfer/freesurfer:7.1.1}"
MRIQC_IMAGE="${MRIQC_IMAGE:-nipreps/mriqc:24.0.2}"
TEMPLATES=(talairach_mixed_with_skull.gca face.gca)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fail() { echo "ERROR: $*" >&2; exit 1; }

DEFACER="" DEFACE_FILES="" MRIQC="" CPAC=yes
while (( $# )); do
    case "$1" in
        --defacer) DEFACER="${2:-}"; shift ;;
        --deface-files) DEFACE_FILES="${2:-}"; shift ;;
        --mriqc) MRIQC=yes ;;
        --no-cpac) CPAC="" ;;
        -h|--help) sed -n '2,14s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) fail "unknown option $1 (--help shows them)" ;;
    esac
    shift
done
case "$DEFACER" in
    freesurfer|pydeface|local|none) ;;
    *) fail "give --defacer freesurfer, pydeface, local or none (--help explains them)" ;;
esac
[[ -n "$CPAC" || "$DEFACER" == none ]] || fail "--no-cpac only goes with --defacer none."
[[ -n "$CPAC" || -n "$MRIQC" ]] || fail "Nothing to do: give --mriqc or leave out --no-cpac."
docker info >/dev/null 2>&1 || fail "can't use Docker: is it running, and may you use it (docker group)?"

# Big downloads sometimes drop; finished layers are kept, so trying again resumes.
pull() {
    if [[ -n "$(docker image ls -q "$1")" ]]; then
        echo "$1 is already downloaded."
        return
    fi
    local attempt
    for attempt in 1 2 3; do
        docker pull "$1" && return
        echo "Download interrupted (attempt $attempt of 3)."
    done
    fail "docker pull $1 failed 3 times. Check the internet connection and run this again."
}

# mri_deface + templates: FreeSurfer-home layout (bin/, average/) or one flat folder.
deface_file() {
    local candidate
    for candidate in "$DEFACE_FILES/$2/$1" "$DEFACE_FILES/$1"; do
        [[ -f "$candidate" ]] && { echo "$candidate"; return; }
    done
    fail "$1 not found in '$DEFACE_FILES' (nor in its $2/ subfolder)."
}

if [[ -n "$CPAC" ]]; then
    echo "== C-PAC ($CPAC_IMAGE, ~15 GB)"
    pull "$CPAC_IMAGE"
fi
if [[ -n "$MRIQC" ]]; then
    echo "== MRIQC ($MRIQC_IMAGE, ~5 GB)"
    pull "$MRIQC_IMAGE"
fi
if [[ "$DEFACER" == none ]]; then
    echo "Done. No defacer image built: T1w images are converted without defacing."
    exit 0
fi

had_freesurfer="$(docker image ls -q "$FREESURFER_IMAGE")"
if [[ "$DEFACER" == freesurfer ]]; then
    echo "== FreeSurfer ($FREESURFER_IMAGE, ~5 GB; only mri_deface is kept)"
    pull "$FREESURFER_IMAGE"
fi

# A fresh build context: the Dockerfile, plus deface_files/ (empty unless --defacer local).
CONTEXT="$(mktemp -d)"
trap 'rm -rf "$CONTEXT"' EXIT
mkdir "$CONTEXT/deface_files"
cp "$HERE/Dockerfile" "$CONTEXT/"
if [[ "$DEFACER" == local ]]; then
    tool="$(deface_file mri_deface bin)"
    cp "$tool" "$CONTEXT/deface_files/"
    for template in "${TEMPLATES[@]}"; do
        file="$(deface_file "$template" average)"
        cp "$file" "$CONTEXT/deface_files/"
    done
fi
echo "== Building $IMAGE with the $DEFACER defacer"
DOCKER_BUILDKIT=1 docker build --build-arg "DEFACER=$DEFACER" --build-arg "CPAC_IMAGE=$CPAC_IMAGE" \
    --build-arg "FREESURFER_IMAGE=$FREESURFER_IMAGE" -t "$IMAGE" "$CONTEXT"
if [[ "$DEFACER" == freesurfer && -z "$had_freesurfer" ]]; then
    docker image rm "$FREESURFER_IMAGE" >/dev/null || true  # frees ~10 GB
fi
echo "Done: $IMAGE is ready (defacer: $DEFACER)."
