"""T1w face removal: FreeSurfer's mri_deface, or pydeface, natively or inside Docker.

Which defacer runs (first match wins):
  1. an explicit folder (--freesurfer): a FreeSurfer home or a folder holding mri_deface
     and its two templates;
  2. $FREESURFER_HOME, if it holds them;
  3. the philani_fmri_process Docker image built by the installer
     (packaging/docker/setup_docker_image.ps1), which carries mri_deface or pydeface.
mri_deface is a Linux binary, so on Windows a folder from 1./2. runs inside a container.
"""

import logging
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fmri_local_process.processing.cpac_docker import (
    CPAC_IMAGE,
    DockerUnavailableError,
    find_docker,
    image_present,
)

log = logging.getLogger(__name__)

DefaceFn = Callable[[Path], str]
"""Defaces a .nii.gz in place and returns the tool's name, for the sidecar's Notes."""

# Built locally by packaging/docker/setup_docker_image.ps1 (FROM CPAC_IMAGE); never pulled.
DEFACE_IMAGE = "philani_fmri_process:latest"
DEFACER_LABEL = "philani.defacer"  # image label: which defacer the image was built with
MRI_DEFACE = "mri_deface"
PYDEFACE = "pydeface"
TOOL_NAMES = {MRI_DEFACE: "FreeSurfer mri_deface", PYDEFACE: "pydeface"}

BRAIN_TEMPLATE = "talairach_mixed_with_skull.gca"
FACE_TEMPLATE = "face.gca"
IMAGE_DEFACE_DIR = "/opt/mri_deface"  # mri_deface + both templates, in DEFACE_IMAGE
DATA_MOUNT = "/data"
TOOLS_MOUNT = "/deface_tools"

NO_DEFACER_HELP = (
    "No defacer found. Re-run the installer and pick a defacer (it builds the "
    f"{DEFACE_IMAGE} Docker image), pass --freesurfer <folder with mri_deface, "
    f"{BRAIN_TEMPLATE} and {FACE_TEMPLATE}>, or pass --no-deface (T1w images are then NOT "
    "defaced)."
)


class DefaceUnavailableError(RuntimeError):
    pass


class DefaceFailedError(OSError):
    """The defacer ran but failed (an OSError, so conversion reports it and carries on)."""


@dataclass(frozen=True)
class MriDefaceFiles:
    root: Path  # the folder searched; the others lie inside it
    exe: Path
    brain_template: Path
    face_template: Path


def find_mri_deface_files(folder: Path) -> MriDefaceFiles | None:
    """mri_deface and its templates in folder: a FreeSurfer home (bin/, average/) or flat."""

    def first(name: str, subfolder: str) -> Path | None:
        return next(
            (path for path in (folder / subfolder / name, folder / name) if path.is_file()), None
        )

    exe = first("mri_deface", "bin")
    brain = first(BRAIN_TEMPLATE, "average")
    face = first(FACE_TEMPLATE, "average")
    if exe is None or brain is None or face is None:
        return None
    return MriDefaceFiles(folder, exe, brain, face)


def defaced_path(t1w: Path) -> Path:
    return t1w.with_name(t1w.name.removesuffix(".nii.gz") + "_defaced.nii.gz")


def run_defacer(cmd: list[str], t1w: Path, tool: str) -> str:
    """Run a defacer that writes defaced_path(t1w), then move its result over t1w."""
    log.debug("Running %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    defaced = defaced_path(t1w)
    if result.returncode != 0 or not defaced.is_file():
        output = (result.stderr.strip() or result.stdout.strip()).splitlines()
        last = output[-1] if output else "no output"
        raise DefaceFailedError(f"{tool} failed (exit {result.returncode}): {last}")
    defaced.replace(t1w)
    # mri_deface's log: <output minus .gz>.log, e.g. x_T1w_defaced.nii.log
    defaced.with_name(defaced.name.removesuffix(".gz") + ".log").unlink(missing_ok=True)
    return tool


def native_mri_deface(files: MriDefaceFiles) -> DefaceFn:
    def deface(t1w: Path) -> str:
        cmd = [str(files.exe), str(t1w), str(files.brain_template), str(files.face_template)]
        return run_defacer([*cmd, str(defaced_path(t1w))], t1w, TOOL_NAMES[MRI_DEFACE])

    return deface


def build_docker_deface_command(
    docker: Path, image: str, t1w: Path, defacer: str, files: MriDefaceFiles | None = None
) -> list[str]:
    """`docker run` defacing t1w with the image's own defacer, or with files mounted in."""
    source = f"{DATA_MOUNT}/{t1w.name}"
    target = f"{DATA_MOUNT}/{defaced_path(t1w).name}"
    cmd = [str(docker), "run", "--rm", "-v", f"{t1w.parent.resolve()}:{DATA_MOUNT}"]
    cmd += ["-w", DATA_MOUNT]
    if defacer == PYDEFACE:
        return [*cmd, "--entrypoint", "pydeface", image, source, "--outfile", target, "--force"]
    if files is None:
        tools = IMAGE_DEFACE_DIR
        exe, brain, face = (
            f"{tools}/{name}" for name in (MRI_DEFACE, BRAIN_TEMPLATE, FACE_TEMPLATE)
        )
    else:
        cmd += ["-v", f"{files.root.resolve()}:{TOOLS_MOUNT}:ro"]
        exe, brain, face = (
            f"{TOOLS_MOUNT}/{path.relative_to(files.root).as_posix()}"
            for path in (files.exe, files.brain_template, files.face_template)
        )
    return [*cmd, "--entrypoint", exe, image, source, brain, face, target]


def docker_deface(
    docker: Path, image: str, defacer: str, files: MriDefaceFiles | None = None
) -> DefaceFn:
    def deface(t1w: Path) -> str:
        cmd = build_docker_deface_command(docker, image, t1w, defacer, files)
        return run_defacer(cmd, t1w, TOOL_NAMES[defacer])

    return deface


def image_defacer(docker: Path, image: str) -> str | None:
    """The defacer DEFACE_IMAGE was built with (its label), or None if absent / unlabelled."""
    label_format = f'{{{{ index .Config.Labels "{DEFACER_LABEL}" }}}}'
    result = subprocess.run(
        [str(docker), "image", "inspect", "--format", label_format, image],
        capture_output=True,
        text=True,
    )
    label = result.stdout.strip()
    return label if result.returncode == 0 and label in TOOL_NAMES else None


def docker_or_raise() -> Path:
    try:
        return find_docker()
    except DockerUnavailableError as e:
        raise DefaceUnavailableError(f"Defacing runs in Docker here, but: {e}") from e


def make_deface(freesurfer: Path | None = None) -> DefaceFn:
    """A function that defaces a .nii.gz in place, found as the module docstring describes."""
    fs_home = os.environ.get("FREESURFER_HOME")
    folder = freesurfer or (Path(fs_home) if fs_home else None)
    files = find_mri_deface_files(folder) if folder is not None else None
    if freesurfer is not None and files is None:
        raise DefaceUnavailableError(
            f"{freesurfer} holds no mri_deface + {BRAIN_TEMPLATE} + {FACE_TEMPLATE} (looked in "
            "the folder itself, and in bin/ and average/ as in a FreeSurfer home)."
        )
    if files is not None and sys.platform != "win32":
        log.info("Defacing with %s", files.exe)
        return native_mri_deface(files)

    docker = docker_or_raise()
    if files is not None:  # a Linux mri_deface on Windows: run it in a Linux container
        image = next((i for i in (DEFACE_IMAGE, CPAC_IMAGE) if image_present(docker, i)), None)
        if image is None:
            raise DefaceUnavailableError(
                f"{files.exe} is a Linux program; running it needs the {CPAC_IMAGE} Docker "
                "image, which isn't downloaded. Run the installer's Docker setup first."
            )
        log.info("Defacing with %s, in Docker (%s)", files.exe, image)
        return docker_deface(docker, image, MRI_DEFACE, files)

    defacer = image_defacer(docker, DEFACE_IMAGE)
    if defacer is None:
        raise DefaceUnavailableError(NO_DEFACER_HELP)
    log.info("Defacing with %s, in Docker (%s)", TOOL_NAMES[defacer], DEFACE_IMAGE)
    return docker_deface(docker, DEFACE_IMAGE, defacer)
