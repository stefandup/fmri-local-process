"""Run C-PAC's standard 'preproc' pipeline (https://fcp-indi.github.io) in its Docker image.

C-PAC delegates to FSL / AFNI / ANTs, which don't run natively on Windows, so the
container does the work; this module only builds the `docker run` call and checks output.
"""

import fnmatch
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CPAC_IMAGE = "fcpindi/c-pac:release-v1.8.7"
CPAC_PRECONFIG = "preproc"
BIDS_MOUNT = "/bids_dataset"
OUTPUT_MOUNT = "/outputs"


# C-PAC refuses to start ('Insufficient resources available for job') when --mem_gb is below
# its biggest step's estimate: 7 GB for 'check_transforms' in the preproc pipeline, plus
# headroom since data-dependent steps (resampling) are estimated at ~6+ GB.
MIN_MEM_GB = 8.0


class DockerUnavailableError(RuntimeError):
    pass


def host_memory_gb() -> float | None:
    """This computer's total RAM in GB, or None if it can't be read."""
    if sys.platform == "win32":
        import ctypes  # Windows-only API; imported here so other platforms never load it

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return status.ullTotalPhys / 1024**3
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except (ValueError, OSError, AttributeError):
        return None


def docker_memory_gb(docker: Path) -> float | None:
    """RAM (GB) Docker lets containers use (Docker Desktop's limit), or None if unknown."""
    return docker_resources(docker)[0]


def docker_resources(docker: Path) -> tuple[float | None, int | None]:
    """(RAM in GB, CPU cores) Docker lets containers use (Docker Desktop's limits); None
    for whatever can't be read."""
    result = subprocess.run(
        [str(docker), "info", "--format", "{{.MemTotal}} {{.NCPU}}"],
        capture_output=True,
        text=True,
    )
    parts = result.stdout.split()
    try:
        mem_gb = int(parts[0]) / 1024**3
    except (ValueError, IndexError):
        mem_gb = None
    try:
        cpus = int(parts[1])
    except (ValueError, IndexError):
        cpus = None
    return mem_gb, cpus


# Running several subjects at once, each in its own container, is how the philani CHPC
# jobs ran (cpacCreateJobs.py: N subjects per node, cores and RAM split evenly). Many
# C-PAC steps use one core, so N subjects x few cores finishes a batch sooner than one
# subject with all cores.
SUGGESTED_MEM_GB = 10.0  # per subject: MIN_MEM_GB plus room for data-dependent steps
MIN_CPUS_PER_SUBJECT = 2
DOCKER_HEADROOM_GB = 2.0  # left for Docker's own VM and the containers' overhead
START_GAP_S = 10.0  # between container starts, so they don't all hit the disk at once


@dataclass(frozen=True)
class ParallelPlan:
    parallel: int  # subjects at once
    n_cpus: int  # per subject
    mem_gb: float  # per subject


def suggest_plan(cpus: int | None, mem_gb: float | None, n_subjects: int = 0) -> ParallelPlan:
    """How many subjects to run at once, and the CPUs / RAM each, to fit cpus cores and
    mem_gb RAM (Docker's limits). n_subjects caps it, when known (0: no cap)."""
    cpus = cpus or 1
    usable_gb = (mem_gb or MIN_MEM_GB) - DOCKER_HEADROOM_GB
    parallel = min(int(usable_gb // SUGGESTED_MEM_GB), cpus // MIN_CPUS_PER_SUBJECT)
    if n_subjects:
        parallel = min(parallel, n_subjects)
    parallel = max(1, parallel)
    mem_each = max(MIN_MEM_GB, math.floor(usable_gb / parallel))
    return ParallelPlan(parallel, max(1, cpus // parallel), float(mem_each))


def parallel_warning(
    plan: ParallelPlan, docker_gb: float | None, docker_cpus: int | None
) -> str | None:
    """Why running plan.parallel subjects at once may go wrong here, or None if it fits."""
    warnings = []
    total_gb = plan.parallel * plan.mem_gb
    if plan.parallel > 1 and docker_gb is not None and total_gb > docker_gb:
        warnings.append(
            f"{plan.parallel} subjects x {plan.mem_gb:g} GB = {total_gb:g} GB, more than the "
            f"{docker_gb:.1f} GB Docker may use: a container can be killed mid-run when "
            "they peak together."
        )
    total_cpus = plan.parallel * plan.n_cpus
    if docker_cpus is not None and total_cpus > docker_cpus:
        warnings.append(
            f"{plan.parallel} subject(s) x {plan.n_cpus} CPUs = {total_cpus} CPUs, more than "
            f"Docker's {docker_cpus}: they slow each other down (harmless, but no faster)."
        )
    return " ".join(warnings) or None


class StartGate:
    """Spaces out container starts by gap_s seconds, across threads."""

    def __init__(self, gap_s: float = START_GAP_S) -> None:
        self.gap_s = gap_s
        self.lock = threading.Lock()
        self.next_start = 0.0

    def wait(self) -> None:
        with self.lock:  # held while sleeping, so starts queue up one gap apart
            delay = self.next_start - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_start = time.monotonic() + self.gap_s


def memory_problem(mem_gb: float, docker_gb: float | None, host_gb: float | None) -> str | None:
    """Why mem_gb can't work on this computer / Docker setup, or None if it looks fine."""
    if host_gb is not None and host_gb < MIN_MEM_GB:
        return (
            f"This computer has {host_gb:.1f} GB RAM; C-PAC preprocessing needs at least "
            f"{MIN_MEM_GB:g} GB, so it can't run here."
        )
    if docker_gb is not None and docker_gb < mem_gb:
        return (
            f"Docker may only use {docker_gb:.1f} GB, less than the {mem_gb:g} GB asked for. "
            "Lower Memory, or raise Docker's limit (Docker Desktop > Settings > Resources, "
            "or 'memory=' in %UserProfile%\\.wslconfig for the WSL 2 backend)."
        )
    return None


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


DOCKER_DESKTOP_ACTIONS = ("start", "restart", "stop")


def docker_desktop(action: str, timeout_s: float = 300) -> str | None:
    """`docker desktop <action>` (start / restart / stop the Docker Desktop engine); None
    when it worked, else why not. Needs Docker Desktop 4.37+ for the `desktop` command."""
    if action not in DOCKER_DESKTOP_ACTIONS:
        raise ValueError(f"unknown Docker Desktop action {action!r}")
    exe = shutil.which("docker")
    if exe is None:
        return "docker not found on PATH. Install Docker Desktop."
    try:
        result = subprocess.run(
            [exe, "desktop", action], capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        return f"'docker desktop {action}' didn't finish within {timeout_s:g} s."
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return f"'docker desktop {action}' failed: {detail}"
    return None


def bold_glob(task: str | None) -> str:
    """Glob for raw BOLD files, of one task (e.g. 'Rest') or of all tasks."""
    return f"*_task-{task}_*bold.nii.gz" if task else "*_bold.nii.gz"


TASK_LABEL = re.compile(r"_task-([a-zA-Z0-9]+)_")


def bids_tasks(bids_root: Path) -> list[str]:
    """Task labels (e.g. 'Rest') of every BOLD run under the sub-*/ folders."""
    return sorted(
        {
            match.group(1)
            for bold in bids_root.glob("sub-*/**/func/*_bold.nii.gz")
            if (match := TASK_LABEL.search(bold.name))
        }
    )


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
    """True when C-PAC wrote a preprocessed BOLD (of task) for every session, and none of
    the outputs it said it would write (its expectedOutputs.yml) are missing.

    C-PAC names outputs '<unique_id>_..._desc-preproc_bold.nii.gz' with unique_id
    '[sub-]<subject>_ses-<n>', under OUTPUT/output/pipeline_<name>/. The preproc BOLD is
    copied out before the other outputs, so a run that dies while copying still has it.
    """
    found = {
        nifti.name.removeprefix("sub-")
        for nifti in (output_root / "output").rglob("*desc-preproc*_bold.nii.gz")
        if task is None or f"_task-{task}_" in nifti.name
    }
    has_bold = bool(sessions) and all(
        any(name.startswith(f"{subject}_ses-{ses}_") for name in found) for ses in sessions
    )
    return has_bold and not any(missing_outputs(output_root, subject, sessions, task).values())


def subject_tasks(bids_root: Path, subject: str) -> list[str]:
    """Task labels of this subject's BOLD runs."""
    return sorted(
        {
            match.group(1)
            for bold in (bids_root / f"sub-{subject}").glob("**/func/*_bold.nii.gz")
            if (match := TASK_LABEL.search(bold.name))
        }
    )


def is_subject_done(bids_root: Path, output_root: Path, subject: str, task: str | None) -> bool:
    """is_preprocessed for task, or, for all tasks, for each of the subject's tasks.

    Checked task by task because 'all tasks' must not count a subject as done when only
    some tasks were run (e.g. an earlier task-Matching-only run): any one task's
    preprocessed BOLD would otherwise satisfy every session.
    """
    tasks = [task] if task else subject_tasks(bids_root, subject)
    return bool(tasks) and all(
        is_preprocessed(output_root, subject, bold_sessions(bids_root, subject, label), label)
        for label in tasks
    )


UNKNOWN_EXPECTED_OUTPUTS = "unknown: the last C-PAC run stopped before listing its outputs"


def expected_outputs_file(output_root: Path, subject: str, session: str) -> Path | None:
    """The newest expectedOutputs.yml C-PAC wrote for this session, or None if never run."""
    unique_id = f"{subject}_ses-{session}"
    candidates = [
        path
        for path in (output_root / "log").glob(f"pipeline_*/*{unique_id}/*_expectedOutputs.yml")
        if path.parent.name.removeprefix("sub-") == unique_id
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def read_expected_outputs(path: Path) -> dict[str, list[str]]:
    """expectedOutputs.yml as {'anat': [glob, ...], 'func': [...]}.

    The file is a flat 'section:' / '- glob' list, so it's read directly rather than
    adding a YAML dependency for it.
    """
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("- ") and current is not None:
            current.append(line[2:].strip().strip("'\""))
        elif line.endswith(":") and not line.startswith(" "):
            current = sections.setdefault(line[:-1].strip(), [])
    return sections


def clear_subject_results(output_root: Path, subject: str) -> list[Path]:
    """Delete this subject's C-PAC working and output folders (so a rerun starts from
    scratch), returning what was deleted. Logs are kept: they explain earlier failures."""
    targets = [
        *(output_root / "working").glob(f"pipeline_*/cpac_sub-{subject}_ses-*"),
        *(output_root / "output").glob(f"pipeline_*/sub-{subject}"),
    ]
    for target in targets:
        shutil.rmtree(target)
    return targets


def short_output_name(pattern: str) -> str:
    """'sub-1*_ses-1*_desc-brain*_mask*' -> 'desc-brain_mask' (for display)."""
    parts = pattern.replace("*", "").split("_")
    return "_".join(part for part in parts if not part.startswith(("sub-", "ses-")))


def missing_outputs(
    output_root: Path, subject: str, sessions: list[str], task: str | None = None
) -> dict[str, list[str]]:
    """Per session, the outputs C-PAC expected to write but didn't, as 'func: desc-x_bold'.

    Sessions C-PAC never started (no expectedOutputs.yml) are left out. With a task, only
    that task's func outputs count (the patterns themselves don't name a task).
    """
    missing: dict[str, list[str]] = {}
    for session in sessions:
        expected_file = expected_outputs_file(output_root, subject, session)
        if expected_file is None:
            continue
        expected = read_expected_outputs(expected_file)
        if not expected:
            # C-PAC empties the file when it starts, so a run that died early leaves it so.
            missing[session] = [UNKNOWN_EXPECTED_OUTPUTS]
            continue
        session_dirs = list(
            (output_root / "output").glob(f"pipeline_*/sub-{subject}/ses-{session}")
        )
        gaps = []
        for section, patterns in expected.items():
            names = [
                file.name
                for session_dir in session_dirs
                for file in (session_dir / section).glob("*")
                if section != "func" or task is None or f"_task-{task}_" in file.name
            ]
            gaps += [
                f"{section}: {short_output_name(pattern)}"
                for pattern in patterns
                if not any(fnmatch.fnmatchcase(name, pattern) for name in names)
            ]
        missing[session] = gaps
    return missing


def preprocessed_bolds(output_root: Path, subject: str) -> list[Path]:
    """Every preprocessed BOLD C-PAC wrote for this subject (all sessions and tasks)."""
    return sorted(
        nifti
        for nifti in (output_root / "output").rglob("*desc-preproc*_bold.nii.gz")
        if nifti.name.removeprefix("sub-").startswith(f"{subject}_")
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


def image_present(docker: Path, image: str) -> bool:
    """True when image is already in the local Docker image store (no pull needed)."""
    result = subprocess.run([str(docker), "image", "inspect", image], capture_output=True)
    return result.returncode == 0


def pull_image(docker: Path, image: str) -> int:
    """`docker pull` image, with docker's own per-layer progress shown on the terminal."""
    return subprocess.run([str(docker), "pull", image]).returncode


NODE_FINISHED = re.compile(r'\[Node\] Finished "([^"]+)"')
# A resumed run reuses finished steps from the working folder; nipype logs each once as
# '[Job N] Cached (node)' instead of running it. Counting them keeps the step count (and
# the saved per-subject estimate) the same whether a run started fresh or resumed.
JOB_CACHED = re.compile(r"\[Job \d+\] Cached \(([^)]+)\)")


def finished_node(line: str) -> str | None:
    """Name of the nipype node a C-PAC console line reports as finished (or reused from
    an earlier run, marked ' (cached)'), else None."""
    if match := NODE_FINISHED.search(line):
        return match.group(1)
    if match := JOB_CACHED.search(line):
        return f"{match.group(1)} (cached)"
    return None


def run_cpac(
    cmd: list[str], console_log: Path, on_line: Callable[[str], None] | None = None
) -> int:
    """Run one C-PAC container, writing its (long) console output to console_log.

    on_line is called with every console line as it arrives, e.g. to drive a progress display.
    """
    log.debug("Running %s", " ".join(cmd))
    console_log.parent.mkdir(parents=True, exist_ok=True)
    with (
        console_log.open("w", encoding="utf-8") as out,
        subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        ) as proc,
    ):
        assert proc.stdout is not None  # stdout=PIPE guarantees it
        for line in proc.stdout:
            out.write(line)
            if on_line is not None:
                on_line(line)
    return proc.returncode


INSUFFICIENT_MEMORY = "Insufficient resources available for job"
NODE_MEMORY = re.compile(r":\s*([\d.]+) GB")
NODE_FAILED = re.compile(r"Node (\S+) failed to run")
CRASH_FILE = re.compile(rf"Saving crash info to {OUTPUT_MOUNT}/(\S+)")
TOOL_ERROR = re.compile(r"\*\* (?:FATAL )?ERROR:\s*(.+)")
PYTHON_ERROR = re.compile(r"^\s*([\w.]*(?:Error|Exception)): (.+)")
TOO_FEW_VOLUMES = re.compile(r"too many for (\d+) retained time points")
OUTPUT_IO_ERROR = f"[Errno 5] Input/output error: '{OUTPUT_MOUNT}/"
ANSI_CODE = re.compile(r"\x1b\[[0-9;]*m")  # terminal colours AFNI writes into its errors


def read_plain_lines(path: Path) -> list[str]:
    """A log's lines with terminal colour codes stripped."""
    return ANSI_CODE.sub("", path.read_text(encoding="utf-8", errors="replace")).splitlines()


def crash_reason(crash_file: Path) -> str | None:
    """The most telling line of a nipype crash file: the tool's own '** ERROR:' if it
    printed one (e.g. AFNI), else the last Python exception."""
    try:
        lines = read_plain_lines(crash_file)
    except OSError:
        return None
    for line in lines:
        if match := TOOL_ERROR.search(line):
            return match.group(1).strip()
    exceptions = [match for line in lines if (match := PYTHON_ERROR.match(line))]
    return f"{exceptions[-1].group(1)}: {exceptions[-1].group(2)}" if exceptions else None


def explain_failure(console_log: Path, output_root: Path) -> str | None:
    """A one-line, human-readable cause of a failed C-PAC run, or None if not recognised."""
    try:
        lines = read_plain_lines(console_log)
    except OSError:
        return None
    for index, line in enumerate(lines):
        if INSUFFICIENT_MEMORY in line:
            needed = [
                float(match.group(1))
                for later in lines[index + 1 :]
                if (match := NODE_MEMORY.search(later))
            ]
            biggest = max(needed, default=0.0)
            return (
                f"Not enough memory: C-PAC's largest step needs {biggest:.1f} GB. Raise Memory "
                f"to at least {math.ceil(biggest) + 1} GB (Docker Desktop must allow it)."
            )
    if any(OUTPUT_IO_ERROR in line for line in lines):
        return (
            "Docker lost access to the output folder mid-run ('Input/output error' writing to "
            f"{OUTPUT_MOUNT}); not a pipeline error. Common causes: the PC slept, Docker "
            "Desktop's file sharing dropped, or antivirus/sync locked files. Run it again: "
            "finished steps are reused from the working folder."
        )
    failed_nodes = list(
        dict.fromkeys(m.group(1) for line in lines if (m := NODE_FAILED.search(line)))
    )
    if not failed_nodes:
        return None
    crash_files = [output_root / m.group(1) for line in lines if (m := CRASH_FILE.search(line))]
    reason = crash_reason(crash_files[0]) if crash_files else None
    text = f"Step '{failed_nodes[0]}' failed"
    text += f": {reason}" if reason else ""
    if reason and (match := TOO_FEW_VOLUMES.search(reason)):
        text += (
            f" -> the BOLD run has only {match.group(1)} usable volumes; likely an incomplete scan."
        )
    if len(failed_nodes) > 1:
        text += f" (+{len(failed_nodes) - 1} more failed step(s))"
    return text


def subject_console_log(output_root: Path, subject: str) -> Path:
    """Where the console output of one subject's C-PAC run is written."""
    return output_root / "log" / f"fmri_preprocess_sub-{subject}.txt"


def preprocess_subject(
    docker: Path,
    bids_root: Path,
    output_root: Path,
    subject: str,
    image: str,
    n_cpus: int,
    mem_gb: float,
    task: str | None = None,
    on_line: Callable[[str], None] | None = None,
) -> str | None:
    """Run C-PAC for one subject: None when every session got a preprocessed BOLD, else why not."""
    sessions = bold_sessions(bids_root, subject, task)
    cmd = build_cpac_command(docker, bids_root, output_root, subject, image, n_cpus, mem_gb, task)
    console_log = subject_console_log(output_root, subject)
    returncode = run_cpac(cmd, console_log, on_line)
    if returncode == 0 and is_subject_done(bids_root, output_root, subject, task):
        return None
    cause = explain_failure(console_log, output_root)
    if cause is None:
        n_missing = sum(map(len, missing_outputs(output_root, subject, sessions, task).values()))
        if returncode:
            cause = f"C-PAC exit {returncode}"
        elif n_missing:
            cause = f"{n_missing} expected output(s) not written"
        else:
            cause = "no preprocessed BOLD written"
    return f"{cause} (full log: {console_log})"
