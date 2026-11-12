"""T1w face removal with FreeSurfer's mri_deface (Linux/macOS only -- not native on Windows)."""

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

DefaceFn = Callable[[Path], None]


class DefaceUnavailableError(RuntimeError):
    pass


def make_mri_deface() -> DefaceFn:
    """Return a function that defaces a .nii.gz in place, or raise if FreeSurfer is missing."""
    exe = shutil.which("mri_deface")
    fs_home = os.environ.get("FREESURFER_HOME")
    if not exe or not fs_home:
        raise DefaceUnavailableError(
            "mri_deface / FREESURFER_HOME not found. Install FreeSurfer, or pass --no-deface "
            "(T1w images are then NOT defaced)."
        )
    average = Path(fs_home) / "average"
    brain_template = average / "talairach_mixed_with_skull.gca"
    face_template = average / "face.gca"

    def deface(t1w: Path) -> None:
        defaced = t1w.with_name(t1w.name.removesuffix(".nii.gz") + "_defaced.nii.gz")
        cmd = [exe, str(t1w), str(brain_template), str(face_template), str(defaced)]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        defaced.replace(t1w)
        defaced.with_name(defaced.name + ".log").unlink(missing_ok=True)

    return deface
