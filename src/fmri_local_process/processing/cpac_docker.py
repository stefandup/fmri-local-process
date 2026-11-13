"""Run C-PAC's standard 'preproc' pipeline (https://fcp-indi.github.io) in its Docker image.

C-PAC delegates to FSL / AFNI / ANTs, which don't run natively on Windows, so the
container does the work; this module only builds the `docker run` call and checks output.
"""

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

CPAC_IMAGE = "fcpindi/c-pac:release-v1.8.7"
CPAC_PRECONFIG = "preproc"
BIDS_MOUNT = "/bids_dataset"
OUTPUT_MOUNT = "/outputs"


class DockerUnavailableError(RuntimeError):
    pass


def find_docker() -> Path:
    """The docker executable, once its daemon answers (Docker Desktop must be running)."""
    exe = shutil.which("docker")
    if exe is None:
        raise DockerUnavailableError("docker not found on PATH. Install Docker Desktop.")
    result = subprocess.run(
        [exe, "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise DockerUnavailableError(
            f"The Docker daemon isn't answering; start Docker Desktop. ({result.stderr.strip()})"
        )
    return Path(exe)


def bold_glob(task: str | None) -> str:
    """Glob for raw BOLD files, of one task (e.g. 'Rest') or of all tasks."""
    return f"*_task-{task}_*bold.nii.gz" if task else "*_bold.nii.gz"


def bids_subjects(bids_root: Path, task: str | None = None) -> list[str]:
    """Subject labels (without 'sub-') of every sub-*/ folder holding a BOLD run (of task)."""
    return sorted(
        sub_dir.name.removeprefix("sub-")
        for sub_dir in bids_root.glob("sub-*")
        if sub_dir.is_dir() and any(sub_dir.rglob(bold_glob(task)))
    )


def bold_sessions(bids_root: Path, subject: str, task: str | None = None) -> list[str]:
    """Session labels (without 'ses-') of this subject that hold a BOLD run (of task)."""
    return sorted(
        ses_dir.name.removeprefix("ses-")
        for ses_dir in (bids_root / f"sub-{subject}").glob("ses-*")
        if any(ses_dir.glob(f"func/{bold_glob(task)}"))
    )


def is_preprocessed(
    output_root: Path, subject: str, sessions: list[str], task: str | None = None
) -> bool:
    """True when C-PAC wrote a preprocessed BOLD (of task) for every session.

    C-PAC names outputs '<unique_id>_..._desc-preproc_bold.nii.gz' with unique_id
    '[sub-]<subject>_ses-<n>', under OUTPUT/output/pipeline_<name>/.
    """
    found = {
        nifti.name.removeprefix("sub-")
        for nifti in (output_root / "output").rglob("*desc-preproc*_bold.nii.gz")
        if task is None or f"_task-{task}_" in nifti.name
    }
    return bool(sessions) and all(
        any(name.startswith(f"{subject}_ses-{ses}_") for name in found) for ses in sessions
    )


def build_cpac_command(
    docker: Path,
    bids_root: Path,
    output_root: Path,
    subject: str,
    image: str,
    n_cpus: int,
    mem_gb: float,
    task: str | None = None,
) -> list[str]:
    """`docker run` for one subject's C-PAC 'preproc' pipeline (only task's BOLD, if given).

    Only sub-<subject>/ and dataset_description.json are mounted: C-PAC walks the whole
    BIDS folder, and crosscheck_pending/ must not be picked up as input.
    """
    sub_dir = f"sub-{subject}"
    mounts = [
        (bids_root / sub_dir, f"{BIDS_MOUNT}/{sub_dir}", ":ro"),
        (
            bids_root / "dataset_description.json",
            f"{BIDS_MOUNT}/dataset_description.json",
            ":ro",
        ),
        (output_root, OUTPUT_MOUNT, ""),
    ]
    cmd = [str(docker), "run", "--rm"]
    for host, container, mode in mounts:
        cmd += ["-v", f"{host.resolve()}:{container}{mode}"]
    cmd += [image, BIDS_MOUNT, OUTPUT_MOUNT, "participant"]
    cmd += ["--preconfig", CPAC_PRECONFIG, "--participant_label", subject]
    cmd += ["--n_cpus", str(n_cpus), "--mem_gb", str(mem_gb), "--tracking_opt-out"]
    if task:
        cmd += ["--bold_label", f"task-{task}"]
    return cmd


def run_cpac(cmd: list[str], console_log: Path) -> int:
    """Run one C-PAC container, writing its (long) console output to console_log."""
    log.debug("Running %s", " ".join(cmd))
    console_log.parent.mkdir(parents=True, exist_ok=True)
    with console_log.open("w", encoding="utf-8") as out:
        result = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, text=True)
    return result.returncode
