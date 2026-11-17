"""Run MRIQC (https://mriqc.readthedocs.io) on the BIDS folder in its Docker image, and read what
it wrote: per-run image quality metrics (IQMs) and HTML reports.

MRIQC is a BIDS App, so it reads the BIDS folder as it is. Like C-PAC (cpac_docker.py), only a
subject's sub-<label>/ and dataset_description.json are mounted, so crosscheck_pending/ is never
read. One container per subject, one after another: progress, stopping and resuming go per
subject. The group step afterwards writes group_<modality>.tsv/.html from the per-run results.

Pinned to 24.0.2: its per-run IQMs are plain JSON. MRIQC 26 writes them as Parquet, which would
need pyarrow to read.

MRIQC 24's output folder:
    sub-<s>/[ses-<n>/]<anat|func>/<scan name>.json   the run's IQMs (+ bids_meta, provenance)
    <scan name>.html                                 the run's visual report
    group_bold.tsv / .html, group_T1w.tsv / .html    the group step
"""

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from fmri_local_process.processing.docker_pull import run_logged

MRIQC_IMAGE = "nipreps/mriqc:24.0.2"
MODALITIES = ("bold", "T1w")
MODALITY_FOLDER = {"bold": "func", "T1w": "anat"}
BIDS_MOUNT = "/data"
OUTPUT_MOUNT = "/out"
NIFTI_SUFFIX = ".nii.gz"
TOOLBOX_LOGS = "toolbox_logs"  # our console logs; MRIQC keeps its own in logs/
MIN_MEM_GB = 4.0  # MRIQC's BOLD workflow peaks at a few GB for typical runs

# Run states
DONE, STALE, NOT_RUN = "done", "stale", "not run"
# Subject states: the run states plus
PARTIAL = "partial"

DOCKER_MISSING, DOCKER_STOPPED, IMAGE_MISSING, READY = "missing", "stopped", "no image", "ready"


class MriqcError(RuntimeError):
    pass


# ---- the scans MRIQC reads -----------------------------------------------------------------------


@dataclass(frozen=True)
class Scan:
    """One BOLD or T1w image in the BIDS folder."""

    relative: Path  # to the BIDS root, e.g. sub-01/ses-1/func/sub-01_ses-1_task-x_bold.nii.gz
    modality: str  # 'bold' or 'T1w'

    @property
    def name(self) -> str:
        """The file name without .nii.gz: MRIQC names its outputs after it."""
        return self.relative.name.removesuffix(NIFTI_SUFFIX)

    @property
    def subject(self) -> str:
        return self.relative.parts[0].removeprefix("sub-")

    @property
    def session(self) -> str:
        """The session label (without 'ses-'), '' for a dataset without sessions."""
        return session_of(self.name)

    @property
    def task(self) -> str:
        """The BOLD task label (without 'task-'), '' for a T1w."""
        match = TASK_LABEL.search(self.name)
        return match.group(1) if match else ""


SESSION_LABEL = re.compile(r"_ses-([a-zA-Z0-9]+)_")
TASK_LABEL = re.compile(r"_task-([a-zA-Z0-9]+)_")


def session_of(name: str) -> str:
    match = SESSION_LABEL.search(name)
    return match.group(1) if match else ""


@dataclass(frozen=True)
class Selection:
    """Which scans MRIQC checks: the T1w or not, and which BOLD tasks."""

    t1w: bool = True
    tasks: tuple[str, ...] | None = None  # BOLD task labels; None: every task, (): no BOLD

    def wants(self, scan: Scan) -> bool:
        if scan.modality == "T1w":
            return self.t1w
        return self.tasks is None or scan.task in self.tasks

    @property
    def empty(self) -> bool:
        return not self.t1w and self.tasks == ()


EVERYTHING = Selection()


def bids_scans(
    bids_root: Path, subject: str | None = None, selection: Selection = EVERYTHING
) -> list[Scan]:
    """Every BOLD and T1w NIfTI under sub-*/ (only this subject's, if given) that selection
    wants."""
    pattern = f"sub-{subject}" if subject else "sub-*"
    scans = []
    for modality in MODALITIES:
        folder = MODALITY_FOLDER[modality]
        for nifti in bids_root.glob(f"{pattern}/**/{folder}/*_{modality}{NIFTI_SUFFIX}"):
            scan = Scan(nifti.relative_to(bids_root), modality)
            if selection.wants(scan):
                scans.append(scan)
    return sorted(scans, key=lambda scan: str(scan.relative))


def bids_subjects(bids_root: Path, selection: Selection = EVERYTHING) -> list[str]:
    """Subject labels (without 'sub-') with a BOLD or T1w scan that selection wants."""
    return sorted({scan.subject for scan in bids_scans(bids_root, selection=selection)})


def bids_tasks(bids_root: Path) -> list[str]:
    """The BOLD task labels in the BIDS folder."""
    return sorted({s.task for s in bids_scans(bids_root) if s.modality == "bold" and s.task})


def has_t1w(bids_root: Path) -> bool:
    return any(scan.modality == "T1w" for scan in bids_scans(bids_root))


def iqm_file(output_root: Path, scan: Scan) -> Path:
    """Where MRIQC writes the scan's IQMs: its BIDS path, in the output folder, as .json."""
    return output_root / scan.relative.with_name(f"{scan.name}.json")


def report_file(output_root: Path, name: str) -> Path:
    """The run's HTML report (name: the scan's file name without .nii.gz)."""
    return output_root / f"{name}.html"


def has_results(output_root: Path) -> bool:
    """True when MRIQC wrote IQMs for any BOLD or T1w run here."""
    return any(
        next(output_root.glob(f"sub-*/**/{MODALITY_FOLDER[m]}/*_{m}.json"), None) is not None
        for m in MODALITIES
    )


def group_report(output_root: Path, modality: str) -> Path | None:
    path = output_root / f"group_{modality}.html"
    return path if path.is_file() else None


def scan_state(bids_root: Path, output_root: Path, scan: Scan) -> str:
    """DONE; STALE when the BIDS file changed after MRIQC read it (trimmed or reconverted);
    NOT_RUN when there are no IQMs for it."""
    iqms = iqm_file(output_root, scan)
    try:
        written = iqms.stat().st_mtime
    except OSError:
        return NOT_RUN
    try:
        changed = (bids_root / scan.relative).stat().st_mtime
    except OSError:
        return DONE
    return STALE if changed > written else DONE


def subject_state(
    bids_root: Path, output_root: Path | None, subject: str, selection: Selection = EVERYTHING
) -> str:
    """DONE when every selected BOLD and T1w has current IQMs; STALE when any is out of date;
    PARTIAL when only some have IQMs; NOT_RUN when none have."""
    states = [
        scan_state(bids_root, output_root, scan) if output_root else NOT_RUN
        for scan in bids_scans(bids_root, subject, selection)
    ]
    if not states or all(s == NOT_RUN for s in states):
        return NOT_RUN
    if STALE in states:
        return STALE
    return DONE if all(s == DONE for s in states) else PARTIAL


def subject_modalities(
    bids_root: Path, subject: str, selection: Selection = EVERYTHING
) -> list[str]:
    """The selected modalities this subject has: MRIQC is only asked for those."""
    found = {scan.modality for scan in bids_scans(bids_root, subject, selection)}
    return [m for m in MODALITIES if m in found]


# ---- Docker -------------------------------------------------------------------------------------


def docker_status(image: str = MRIQC_IMAGE) -> tuple[str, str, Path | None]:
    """(state, what to tell the user, docker) with state one of DOCKER_MISSING, DOCKER_STOPPED,
    IMAGE_MISSING, READY. Asks Docker, so call it off the GUI thread."""
    exe = shutil.which("docker")
    if exe is None:
        return DOCKER_MISSING, "Docker isn't installed: MRIQC runs in Docker Desktop.", None
    result = subprocess.run(
        [exe, "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return DOCKER_STOPPED, "Docker isn't running: start Docker Desktop to run MRIQC.", None
    docker = Path(exe)
    if subprocess.run([exe, "image", "inspect", image], capture_output=True).returncode != 0:
        return (
            IMAGE_MISSING,
            f"The MRIQC image ({image}, ~5 GB download) isn't installed: download it here, or "
            "tick MRIQC when running the installer.",
            docker,
        )
    return READY, "", docker


DOCKER_HEADROOM_GB = 2.0  # left for Docker's own VM, as for C-PAC
DEFAULT_MEM_GB = 8.0


def suggest_resources(cpus: int | None, mem_gb: float | None) -> tuple[int, float]:
    """(CPUs, GB) for MRIQC within Docker's limits: one subject runs at a time, so all of
    Docker's CPUs, and its memory less some headroom (whole GB, at least MIN_MEM_GB)."""
    suggested_cpus = max(1, cpus or 1)
    if mem_gb is None:
        return suggested_cpus, DEFAULT_MEM_GB
    return suggested_cpus, max(MIN_MEM_GB, float(int(mem_gb - DOCKER_HEADROOM_GB)))


def container_name(subject: str) -> str:
    return f"fmri_mriqc_sub-{subject}"


def build_participant_command(
    docker: Path,
    bids_root: Path,
    output_root: Path,
    subject: str,
    modalities: Sequence[str],
    n_procs: int,
    mem_gb: float,
    image: str = MRIQC_IMAGE,
    tasks: Sequence[str] = (),
) -> list[str]:
    """`docker run` of MRIQC's participant level for one subject (only these BOLD tasks, if
    given).

    --no-sub: never upload IQMs to MRIQC's public web API; --notrack: no usage telemetry.
    -v: log each finished pipeline step (nipype's '[Node] Finished'), for the progress bar.
    """
    sub_dir = f"sub-{subject}"
    cmd = [str(docker), "run", "--rm", "--name", container_name(subject)]
    cmd += ["-v", f"{(bids_root / sub_dir).resolve()}:{BIDS_MOUNT}/{sub_dir}:ro"]
    cmd += [
        "-v",
        f"{(bids_root / 'dataset_description.json').resolve()}:"
        f"{BIDS_MOUNT}/dataset_description.json:ro",
    ]
    cmd += ["-v", f"{output_root.resolve()}:{OUTPUT_MOUNT}"]
    cmd += [image, BIDS_MOUNT, OUTPUT_MOUNT, "participant", "--participant-label", subject]
    cmd += ["-m", *modalities, "--nprocs", str(n_procs), "--mem_gb", f"{mem_gb:g}"]
    if tasks and "bold" in modalities:
        cmd += ["--task-id", *tasks]
    return [*cmd, "-v", "--no-sub", "--notrack"]


def build_group_command(
    docker: Path, bids_root: Path, output_root: Path, image: str = MRIQC_IMAGE
) -> list[str]:
    """`docker run` of MRIQC's group level: group_<modality>.tsv/.html from the IQMs."""
    cmd = [str(docker), "run", "--rm", "--name", "fmri_mriqc_group"]
    cmd += [
        "-v",
        f"{(bids_root / 'dataset_description.json').resolve()}:"
        f"{BIDS_MOUNT}/dataset_description.json:ro",
    ]
    cmd += ["-v", f"{output_root.resolve()}:{OUTPUT_MOUNT}"]
    return [*cmd, image, BIDS_MOUNT, OUTPUT_MOUNT, "group", "--no-sub", "--notrack"]


def subject_log(output_root: Path, subject: str) -> Path:
    """Where the console output of one subject's MRIQC run is written."""
    return output_root / TOOLBOX_LOGS / f"mriqc_sub-{subject}.txt"


def stop_container(docker: Path, name: str, grace_s: int = 10) -> str | None:
    """`docker stop` a container; None when it worked, else docker's complaint."""
    try:
        result = subprocess.run(
            [str(docker), "stop", "-t", str(grace_s), name],
            capture_output=True,
            text=True,
            timeout=grace_s + 60,
        )
    except subprocess.TimeoutExpired:
        return "'docker stop' didn't answer in time."
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout).strip() or "'docker stop' failed"


ERROR_LINE = re.compile(r"(?:\bERROR\b|Error:|Exception:|error:)\s*(.+)")
MEMORY_HINTS = ("MemoryError", "Killed", "exit code 137", "Cannot allocate memory")


def explain_failure(log_path: Path, returncode: int) -> str:
    """A one-line cause of a failed run, from its console log."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    if returncode == 137 or any(hint in line for line in lines for hint in MEMORY_HINTS):
        return "ran out of memory: give MRIQC more (Memory), or raise Docker Desktop's limit"
    errors = [match.group(1).strip() for line in lines if (match := ERROR_LINE.search(line))]
    reason = errors[-1] if errors else f"MRIQC exit {returncode}"
    return f"{reason} (full log: {log_path})"


def run_subject(
    docker: Path,
    bids_root: Path,
    output_root: Path,
    subject: str,
    n_procs: int,
    mem_gb: float,
    on_line: Callable[[str], None] | None = None,
    image: str = MRIQC_IMAGE,
    selection: Selection = EVERYTHING,
) -> str | None:
    """MRIQC for one subject's selected scans: None when each got current IQMs, else why not."""
    modalities = subject_modalities(bids_root, subject, selection)
    if not modalities:
        return "no selected BOLD or T1w scans in BIDS"
    cmd = build_participant_command(
        docker, bids_root, output_root, subject, modalities, n_procs, mem_gb, image,
        selection.tasks or (),
    )  # fmt: skip
    log_path = subject_log(output_root, subject)
    returncode = run_logged(cmd, log_path, on_line)
    state = subject_state(bids_root, output_root, subject, selection)
    if returncode == 0 and state == DONE:
        return None
    if returncode == 0:
        missing = [
            scan.name
            for scan in bids_scans(bids_root, subject, selection)
            if scan_state(bids_root, output_root, scan) != DONE
        ]
        return f"no IQMs written for {', '.join(missing)} (full log: {log_path})"
    return explain_failure(log_path, returncode)


# ---- reading the IQMs ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Metric:
    key: str  # in MRIQC's JSON
    label: str
    better: str | None  # 'lower' / 'higher': which way is better; None: no direction
    help: str


METRICS: dict[str, tuple[Metric, ...]] = {
    "bold": (
        Metric("fd_mean", "FD mean (mm)", "lower", "Mean framewise displacement: head motion."),
        Metric("fd_perc", "FD > 0.2 mm (%)", "lower", "Percentage of volumes with FD > 0.2 mm."),
        Metric("dvars_std", "DVARS (std.)", "lower", "Standardised DVARS: frame-to-frame change."),
        Metric("tsnr", "tSNR", "higher", "Temporal signal-to-noise ratio (median in the brain)."),
        Metric("snr", "SNR", "higher", "Spatial signal-to-noise ratio of the mean BOLD."),
        Metric("gsr_y", "Ghost ratio (y)", "lower", "Ghost-to-signal ratio, phase direction."),
        Metric("aor", "Outliers (AFNI)", "lower", "Mean fraction of outlier voxels per volume."),
        Metric("aqi", "Quality index", "lower", "AFNI's quality index: mean distance to median."),
        Metric("efc", "EFC", "lower", "Entropy focus criterion: ghosting and blurring."),
        Metric("fwhm_avg", "Smoothness (voxels)", None, "Average FWHM smoothness, in voxels."),
        Metric("size_t", "Volumes", None, "Number of volumes MRIQC read."),
    ),
    "T1w": (
        Metric("cjv", "CJV", "lower", "Coefficient of joint variation of GM and WM: noise, INU."),
        Metric("cnr", "CNR", "higher", "Contrast-to-noise ratio between GM and WM."),
        Metric("snr_total", "SNR", "higher", "Signal-to-noise ratio over the tissues."),
        Metric("efc", "EFC", "lower", "Entropy focus criterion: ghosting and blurring."),
        Metric("fber", "FBER", "higher", "Foreground (head) to background energy ratio."),
        Metric("qi_1", "Artefacts (QI1)", "lower", "Fraction of background voxels with artefacts."),
        Metric("inu_med", "INU median", None, "Median intensity non-uniformity (1 = none)."),
        Metric("fwhm_avg", "Smoothness (voxels)", None, "Average FWHM smoothness, in voxels."),
    ),
}
MIN_RUNS_FOR_OUTLIERS = 5  # fewer: a quartile range says nothing
IQR_FACTOR = 1.5  # Tukey's fences


@dataclass
class RunIqms:
    name: str  # the scan's file name without .nii.gz
    modality: str
    subject: str
    values: dict[str, float] = field(default_factory=dict)
    state: str = DONE  # STALE when the BIDS file changed since
    report: Path | None = None

    @property
    def session(self) -> str:
        return session_of(self.name)

    @property
    def label(self) -> str:
        """The run's name without the subject and session: 'task-rest_run-1_bold'."""
        parts = self.name.split("_")
        return "_".join(p for p in parts if not p.startswith(("sub-", "ses-"))) or self.name


def by_subject_session(runs: Sequence[RunIqms]) -> dict[str, dict[str, list[RunIqms]]]:
    """{subject: {session: [runs]}}, in sorted order, for the grouped display."""
    grouped: dict[str, dict[str, list[RunIqms]]] = {}
    for run in sorted(runs, key=lambda r: (r.subject, r.session, r.name)):
        grouped.setdefault(run.subject, {}).setdefault(run.session, []).append(run)
    return grouped


def read_iqms(path: Path) -> dict[str, float]:
    """The numeric IQMs of one MRIQC JSON (bids_meta and provenance are left out)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: not an IQM file")
    return {
        key: float(value)
        for key, value in data.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def collect_iqms(
    output_root: Path, modality: str, bids_root: Path | None = None
) -> tuple[list[RunIqms], list[str]]:
    """Every run of this modality MRIQC wrote IQMs for, and the files it couldn't read.
    Read from the output folder alone, so results show without the BIDS folder too; with it,
    runs whose BIDS file changed since are marked STALE."""
    runs, unreadable = [], []
    folder = MODALITY_FOLDER[modality]
    for path in sorted(output_root.glob(f"sub-*/**/{folder}/*_{modality}.json")):
        try:
            values = read_iqms(path)
        except (OSError, ValueError) as e:
            unreadable.append(f"{path.name}: {e}")
            continue
        relative = path.relative_to(output_root)
        scan = Scan(relative.with_name(f"{path.stem}{NIFTI_SUFFIX}"), modality)
        state = scan_state(bids_root, output_root, scan) if bids_root is not None else DONE
        report = report_file(output_root, scan.name)
        runs.append(
            RunIqms(
                scan.name,
                modality,
                scan.subject,
                values,
                state,
                report if report.is_file() else None,
            )
        )
    return runs, unreadable


RUN_ENTITY = re.compile(r"_?run-[a-zA-Z0-9]+")


def comparison_group(run: RunIqms) -> str:
    """Runs compared with each other for outliers: the same kind of scan (task, acquisition),
    whatever the subject, session or run number. A rest run's tSNR says nothing about a
    task run's."""
    return RUN_ENTITY.sub("", run.label).strip("_")


def flag_outliers(runs: Sequence[RunIqms]) -> dict[str, set[str]]:
    """{run name: metric keys where it is an outlier among the runs of its comparison group}."""
    groups: dict[str, list[RunIqms]] = {}
    for run in runs:
        groups.setdefault(comparison_group(run), []).append(run)
    flagged: dict[str, set[str]] = {run.name: set() for run in runs}
    for members in groups.values():
        for metric in METRICS[members[0].modality]:
            values = [run.values.get(metric.key) for run in members]
            for run, flag in zip(members, outlier_flags(values, metric.better), strict=True):
                if flag:
                    flagged[run.name].add(metric.key)
    return flagged


def outlier_flags(values: Sequence[float | None], better: str | None) -> list[bool]:
    """Which values are outliers within the dataset (Tukey's fences: beyond 1.5 x IQR from
    the quartiles), only on the worse side when the metric has a better direction. Nothing
    is flagged with fewer than MIN_RUNS_FOR_OUTLIERS values."""
    numbers = np.array([v for v in values if v is not None], dtype=float)
    if numbers.size < MIN_RUNS_FOR_OUTLIERS:
        return [False] * len(values)
    q1, q3 = np.percentile(numbers, [25, 75])
    low, high = q1 - IQR_FACTOR * (q3 - q1), q3 + IQR_FACTOR * (q3 - q1)
    flags = []
    for value in values:
        if value is None:
            flags.append(False)
        elif better == "lower":
            flags.append(value > high)
        elif better == "higher":
            flags.append(value < low)
        else:
            flags.append(value < low or value > high)
    return flags
