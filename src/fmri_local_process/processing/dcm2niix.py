"""Locate and run the dcm2niix executable (https://github.com/rordenlab/dcm2niix)."""

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

EXE_NAME = "dcm2niix.exe" if sys.platform == "win32" else "dcm2niix"
PLATFORM = {"win32": "win", "darwin": "mac"}.get(sys.platform, "linux")
# Fetched by tools/fetch_dcm2niix.*; src/fmri_local_process/processing/ -> repo root is parents[3]
REPO_THIRD_PARTY = (
    Path(__file__).resolve().parents[3] / "third_party" / f"dcm2niix_{PLATFORM}" / EXE_NAME
)


class Dcm2niixNotFoundError(FileNotFoundError):
    pass


def _bundled_exe() -> Path | None:
    """The copy PyInstaller bundles next to the frozen app (see build.ps1)."""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    return Path(bundle_dir) / "dcm2niix" / EXE_NAME if bundle_dir else None


def find_dcm2niix(explicit: Path | None = None) -> Path:
    """Explicit path > $DCM2NIIX > frozen bundle > repo third_party/ > PATH."""
    env_path = os.environ.get("DCM2NIIX")
    candidates = [
        explicit,
        Path(env_path) if env_path else None,
        _bundled_exe(),
        REPO_THIRD_PARTY,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate

    on_path = shutil.which("dcm2niix")
    if on_path:
        return Path(on_path)

    raise Dcm2niixNotFoundError(
        "dcm2niix not found. Run tools/fetch_dcm2niix.ps1, set $DCM2NIIX, or pass --dcm2niix."
    )


def run_dcm2niix(exe: Path, dicom_dir: Path, out_dir: Path, stem: str) -> None:
    """Convert one DICOM series folder to out_dir/<stem>.nii.gz + .json.

    Flags match philaniToBIDS.sh: BIDS sidecar, gzip, no crop, single-file mode.
    """
    cmd = [str(exe), "-b", "y", "-z", "y", "-x", "n", "-s", "y"]
    cmd += ["-o", str(out_dir), "-f", stem, str(dicom_dir)]
    log.debug("Running %s", " ".join(cmd))
    subprocess.run(cmd, check=True, capture_output=True, text=True)
