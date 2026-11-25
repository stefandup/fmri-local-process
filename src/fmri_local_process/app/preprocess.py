"""Preprocess BIDS (C-PAC): the batch job, each subject's status, and the texts. The run itself
is the same per-subject call the fmri_preprocess CLI makes (`preprocess_subject`)."""

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from fmri_local_process.app.colors import (
    ERROR_COLOR,
    OK_COLOR,
    RUNNING_COLOR,
    UNKNOWN_COLOR,
    WARNING_COLOR,
)
from fmri_local_process.app.settings import Settings, is_on
from fmri_local_process.processing.cpac_docker import (
    CPAC_IMAGE,
    MIN_MEM_GB,
    RESTART_MARKER,
    START_GAP_S,
    DockerUnavailableError,
    HangPolicy,
    ParallelPlan,
    StartGate,
    bids_subjects,
    bold_sessions,
    clear_subject_results,
    clear_working_volume,
    docker_memory_gb,
    docker_resources,
    explain_failure,
    find_docker,
    finished_node,
    host_memory_gb,
    image_present,
    is_subject_done,
    mark_stopped,
    memory_problem,
    missing_outputs,
    parallel_warning,
    preprocess_subject,
    run_cpac,
    stop_containers,
    subject_console_log,
    subject_tasks_in,
    suggest_plan,
    was_stopped,
)
from fmri_local_process.processing.cpac_outputs import bids_entities, read_tsv
from fmri_local_process.processing.session_sync import (
    SessionChange,
    adopt_sessions,
    rename_outputs,
    session_changes,
)

log = logging.getLogger(__name__)

SETTINGS_APP = "FmriPreprocess"
LOG_TAIL_LINES = 400
MAX_MISSING_SHOWN = 12
STATUS_ICON = {
    "done": ("✓", OK_COLOR),
    "partial": ("◐", WARNING_COLOR),
    "failed": ("✗", ERROR_COLOR),
    "stopped": ("■", WARNING_COLOR),
    "renumbered": ("⇄", WARNING_COLOR),
    "running": ("▶", RUNNING_COLOR),
    "not run": ("○", UNKNOWN_COLOR),
}
STATUS_LEGEND = (
    "✓ every session has a preprocessed BOLD (of each ticked task) and every output C-PAC "
    "expected\n"
    "◐ (several tasks ticked) some are preprocessed, others not yet\n"
    "✗ C-PAC ran (a log exists) but its output is incomplete\n"
    "■ stopped by you before it finished; running it again resumes it\n"
    "⇄ the BIDS sessions were renumbered since it was preprocessed (an earlier session was "
    "added): its output carries the old numbers. Run it again (old results are cleared "
    "first) or rename its output -- select it for both options\n"
    "▶ running now\n"
    "○ not run yet"
)
# Run mode -> dropdown label. Every mode reuses C-PAC's working folder except "scratch".
RUN_MODES = {
    "skip": "Skip done, resume the rest",
    "resume": "Resume all (reuse finished steps)",
    "scratch": "Rerun from scratch (delete previous results)",
}


def task_names(tasks: list[str]) -> str:
    """['Rest', 'Matching'] -> 'task-Rest, task-Matching'; [] -> 'all tasks'."""
    return ", ".join(f"task-{label}" for label in tasks) or "all tasks"


@dataclass
class RunSettings:
    bids_root: Path
    output_root: Path
    subjects: list[str]
    tasks: list[str]  # BOLD task labels to run; [] for all
    n_cpus: int  # per subject
    mem_gb: float  # per subject
    mode: str = "skip"  # a RUN_MODES key
    image: str = CPAC_IMAGE
    parallel: int = 1  # subjects at once, each in its own container
    hang: HangPolicy = field(default_factory=HangPolicy)
    keep_working: bool = False  # keep a finished subject's working folder in Docker


@dataclass
class RunSummary:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False


def read_log_tail(path: Path, n_lines: int = LOG_TAIL_LINES) -> str:
    """Last n_lines of a (possibly multi-MB) C-PAC console log."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    skipped = len(lines) - n_lines
    head = (
        [f"... {skipped} earlier lines, open the file for all of it ...", ""] if skipped > 0 else []
    )
    return "\n".join(head + lines[-n_lines:])


# ---- jobs ---------------------------------------------------------------------------------------


class PreprocessListener(Protocol):
    """What the job tells the UI while it runs (called on the job's threads)."""

    def on_message(self, text: str, is_error: bool, /) -> None: ...
    def on_subject_started(self, index: int, total: int, subject: str, /) -> None: ...
    def on_step(self, subject: str, node: str, steps_done: int, /) -> None: ...
    def on_subject_finished(self, subject: str, steps: int, preprocessed: bool, /) -> None: ...
    def on_batch_done(self, result: RunSummary | str, /) -> None: ...  # or an error message


class PreprocessJob:
    """Runs the batch: Docker check, one-time image pull, then one C-PAC container per
    subject, run.parallel of them at a time."""

    def __init__(self, run: RunSettings, listener: PreprocessListener) -> None:
        self.run_settings = run
        self.listener = listener
        self.stop_requested = False
        self.docker: Path | None = None
        # Stop now: `docker stop` the running containers. One that is still starting (no
        # container to stop yet) is stopped at its first console line instead.
        self.stop_now_requested = False
        self.lock = threading.Lock()
        self.active: set[str] = set()  # subjects whose container is (about to be) running
        self.stopping: set[str] = set()  # ... and of those, the ones docker was told to stop

    def stop(self) -> None:
        """Let the running subject(s) finish and start no new ones."""
        self.stop_requested = True

    def run(self) -> None:
        run = self.run_settings
        try:
            docker = find_docker()
        except DockerUnavailableError as e:
            self.listener.on_batch_done(str(e))
            return
        self.docker = docker
        problem = memory_problem(run.mem_gb, docker_memory_gb(docker), host_memory_gb())
        if problem is not None:
            self.listener.on_batch_done(problem)
            return
        if not image_present(docker, run.image):
            self.listener.on_message(
                f"Pulling C-PAC image {run.image} (~15 GB, one-time; can take 30+ min)...", False
            )
            pull_log = run.output_root / "log" / "docker_pull.txt"
            pull_cmd = [str(docker), "pull", run.image]
            if run_cpac(pull_cmd, pull_log, self.relay_pull_line) != 0:
                self.listener.on_batch_done(f"docker pull {run.image} failed; see {pull_log}")
                return

        summary = RunSummary()
        gate = StartGate(START_GAP_S if run.parallel > 1 else 0.0)
        # Each subject runs on a pool thread; the listener passes its calls to the UI thread.
        with ThreadPoolExecutor(max_workers=run.parallel) as pool:
            for index, subject in enumerate(run.subjects):
                pool.submit(self.run_subject, docker, index, subject, gate, summary)
        self.listener.on_batch_done(summary)

    def run_subject(
        self, docker: Path, index: int, subject: str, gate: StartGate, summary: RunSummary
    ) -> None:
        run = self.run_settings
        if self.stop_requested:
            summary.stopped = True
            return
        done = is_subject_done(run.bids_root, run.output_root, subject, run.tasks)
        if run.mode == "skip" and done:
            summary.skipped.append(subject)
            self.listener.on_subject_finished(subject, 0, False)
            return
        gate.wait()
        with self.lock:  # under the lock, so stop_now() can't miss a starting subject
            if self.stop_requested:  # pressed while waiting for a start slot
                summary.stopped = True
                return
            self.active.add(subject)
        self.listener.on_subject_started(index, len(run.subjects), subject)
        steps_done = 0

        def on_line(line: str) -> None:
            nonlocal steps_done
            if self.stop_now_requested and subject not in self.stopping:
                self.stop_container(subject)
            if line == RESTART_MARKER:  # the resumed run counts its steps again
                steps_done = 0
                self.listener.on_step(subject, "restarted after a hang", 0)
                return
            node = finished_node(line)
            if node is not None:
                steps_done += 1
                self.listener.on_step(subject, node, steps_done)

        try:
            if run.mode == "scratch":
                removed = [
                    *clear_subject_results(run.output_root, subject),
                    *clear_working_volume(docker, run.image, run.output_root, subject),
                ]
                self.listener.on_message(
                    f"sub-{subject}: deleted {len(removed)} previous working/output folder(s)",
                    False,
                )
            failure = preprocess_subject(
                docker,
                run.bids_root,
                run.output_root,
                subject,
                run.image,
                run.n_cpus,
                run.mem_gb,
                run.tasks,
                on_line,
                hang=run.hang,
                tell=lambda text: self.listener.on_message(
                    text, any(word in text for word in ("hung", "lost contact", "didn't come"))
                ),
                may_restart=lambda: not self.stop_now_requested,
                keep_working=run.keep_working,
            )
        except Exception as e:  # noqa: BLE001 -- one subject's crash mustn't end the batch
            log.exception("sub-%s crashed", subject)
            failure = f"{type(e).__name__}: {e}"
        with self.lock:
            self.active.discard(subject)
            stopped = subject in self.stopping
        if failure is None:
            summary.done.append(subject)
            self.listener.on_message(f"sub-{subject}: preprocessed ({steps_done} steps)", False)
        elif stopped:
            mark_stopped(subject_console_log(run.output_root, subject))
            summary.stopped = True
            self.listener.on_message(
                f"sub-{subject}: stopped after {steps_done} steps; running it again resumes it",
                False,
            )
        else:
            summary.failed.append((subject, failure))
            self.listener.on_message(f"sub-{subject} FAILED: {failure}", True)
        self.listener.on_subject_finished(subject, steps_done, failure is None)

    def stop_container(self, subject: str) -> None:
        """`docker stop` one subject's container (blocks up to ~10 s)."""
        with self.lock:
            if self.docker is None or subject not in self.active or subject in self.stopping:
                return
            self.stopping.add(subject)
        error = stop_containers(self.docker, [subject])
        if error is not None and "No such container" in error:
            with self.lock:  # still starting: stopped at its first console line instead
                self.stopping.discard(subject)
        elif error is not None:
            self.listener.on_message(f"sub-{subject}: couldn't stop its container: {error}", True)

    def stop_now(self) -> list[str]:
        """Start no new subjects and stop the running containers, freeing their CPUs and
        memory; returns the subjects stopped (blocks on docker: call it off the UI thread)."""
        with self.lock:
            self.stop_requested = True
            self.stop_now_requested = True
            running = sorted(self.active)
        for subject in running:
            self.stop_container(subject)
        return running

    def relay_pull_line(self, line: str) -> None:
        # Without a TTY docker prints one line per layer event; only completions matter.
        text = line.strip()
        if text.endswith("Pull complete") or text.startswith(("Digest:", "Status:")):
            self.listener.on_message(f"docker pull: {text}", False)


@dataclass
class DockerProbe:
    """What Docker said about its limits and the C-PAC image."""

    gb: float | None = None
    cpus: int | None = None
    error: str = ""  # why unknown
    has_image: bool | None = None  # None: unknown


def probe_docker() -> DockerProbe:
    """Slow when Docker is starting: call it off the UI thread."""
    try:
        docker = find_docker()
    except DockerUnavailableError as e:
        return DockerProbe(error=str(e))
    gb, cpus = docker_resources(docker)
    return DockerProbe(gb, cpus, "", image_present(docker, CPAC_IMAGE))


def rename_subject(
    bids_root: Path, output_root: Path, subject: str, changes: list[SessionChange]
) -> int:
    """Give a subject's output its new session numbers and clear its C-PAC working data: how
    many paths were renamed. DockerUnavailableError / OSError when it can't."""
    docker = find_docker()
    renamed = rename_outputs(bids_root, output_root, subject, changes)
    clear_working_volume(docker, CPAC_IMAGE, output_root, subject)
    return len(renamed)


def mark_all_stopped(output_root: Path, subjects: list[str]) -> None:
    """Closing: the worker threads won't get to it."""
    for subject in subjects:
        mark_stopped(subject_console_log(output_root, subject))


# ---- settings -----------------------------------------------------------------------------------


@dataclass
class RunChoices:
    """The remembered run settings (the window's and the terminal app's form)."""

    plan: ParallelPlan
    mode: str
    hang: HangPolicy
    keep_working: bool
    unticked_tasks: set[str]


def read_choices(settings: Settings) -> RunChoices:
    default = HangPolicy()
    mode = str(settings.value("run_mode", "skip"))
    unticked = str(settings.value("unticked_tasks", "") or "")
    return RunChoices(
        ParallelPlan(
            int(str(settings.value("parallel", 1))),
            int(str(settings.value("n_cpus", 1))),
            max(MIN_MEM_GB, float(str(settings.value("mem_gb", 0)))),
        ),
        # A deleting mode is never restored as the default for the next session.
        mode if mode in RUN_MODES and mode != "scratch" else "skip",
        HangPolicy(
            int(float(str(settings.value("hang_minutes", default.minutes)))),
            str(settings.value("hang_action", default.action)),
            int(str(settings.value("max_restarts", default.max_restarts))),
        ),
        is_on(settings.value("keep_working", "false")),
        set(filter(None, unticked.split(","))),
    )


def save_choices(settings: Settings, run: RunSettings, plan: ParallelPlan) -> None:
    settings.setValue("parallel", plan.parallel)
    settings.setValue("n_cpus", plan.n_cpus)
    settings.setValue("mem_gb", plan.mem_gb)
    settings.setValue("run_mode", run.mode)
    settings.setValue("hang_minutes", int(run.hang.minutes))
    settings.setValue("hang_action", run.hang.action)
    settings.setValue("max_restarts", run.hang.max_restarts)
    settings.setValue("keep_working", run.keep_working)


def save_unticked_tasks(settings: Settings, unticked: set[str]) -> None:
    settings.setValue("unticked_tasks", ",".join(sorted(unticked)))


def task_filter(labels: list[str], unticked: set[str]) -> list[str]:
    """The task filter for C-PAC and the roster: the ticked tasks, or [] (every task) when all
    are ticked."""
    ticked = [label for label in labels if label not in unticked]
    return [] if ticked == labels else ticked


def estimate_key(tasks: list[str]) -> str:
    return f"steps_per_subject/{'+'.join(tasks) or 'all'}"


def saved_estimate(settings: Settings, tasks: list[str]) -> int | None:
    """Steps the last complete run of a subject took (with these tasks), if known."""
    return int(str(settings.value(estimate_key(tasks), 0))) or None


# ---- subjects -----------------------------------------------------------------------------------


def roster(
    bids_root: Path | None, output_root: Path | None, tasks: list[str], ticked_any: bool
) -> list[str]:
    subjects = bids_subjects(bids_root, tasks) if bids_root and ticked_any else []
    if bids_root is not None and output_root is not None:
        for subject in subjects:  # output from before sessions were recorded
            adopt_sessions(bids_root, output_root, subject)
    return subjects


def tasks_to_do(
    bids_root: Path | None, output_root: Path | None, subject: str, tasks: list[str]
) -> list[str]:
    """This subject's ticked tasks that aren't fully preprocessed yet."""
    if bids_root is None or output_root is None:
        return []
    return [
        label
        for label in subject_tasks_in(bids_root, subject, tasks)
        if not is_subject_done(bids_root, output_root, subject, label)
    ]


def subject_status(
    bids_root: Path | None, output_root: Path | None, subject: str, tasks: list[str], running: bool
) -> str:
    """A STATUS_ICON key."""
    if running:
        return "running"
    if bids_root is None or output_root is None:
        return "not run"
    if session_changes(bids_root, output_root, subject):
        return "renumbered"
    if is_subject_done(bids_root, output_root, subject, tasks):
        return "done"
    if tasks_to_do(bids_root, output_root, subject, tasks) != subject_tasks_in(
        bids_root, subject, tasks
    ):
        return "partial"  # several tasks ticked, some already preprocessed on their own
    console_log = subject_console_log(output_root, subject)
    if console_log.is_file():
        return "stopped" if was_stopped(console_log) else "failed"
    return "not run"


# ---- texts --------------------------------------------------------------------------------------


def notices(
    bids_root: Path | None,
    output_root: Path | None,
    labels: list[str],
    ticked_any: bool,
    cpac_missing: bool,
) -> list[str]:
    """What is missing to preprocess: folders, BOLD runs, ticked tasks, the C-PAC image."""
    messages = []
    if cpac_missing:
        messages.append(
            f"The C-PAC image ({CPAC_IMAGE}, ~15 GB) isn't downloaded yet: click "
            "'Download the C-PAC image' below (once per computer), or tick C-PAC in the "
            "installer."
        )
    if bids_root is None:
        messages.append("No BIDS folder chosen: Browse for it below.")
    elif not labels:
        messages.append(
            f"No BOLD runs in {bids_root} yet: convert the DICOMs on the Setup & "
            "Convert to BIDS tab first."
        )
    elif not ticked_any:
        messages.append("No task ticked: tick the BOLD tasks to preprocess below.")
    if output_root is None:
        messages.append("No C-PAC output folder chosen: Browse for one below.")
    return messages


def summary_text(
    bids_root: Path | None,
    output_root: Path | None,
    tasks: list[str],
    ticked_any: bool,
    statuses: dict[str, str],
) -> str:
    if bids_root is None:
        return "Choose a BIDS folder and an output folder."
    if not ticked_any:
        return "No tasks ticked: tick at least one task."
    what = f"{task_names(tasks)} BOLD" if tasks else "BOLD runs"
    counts = {status: list(statuses.values()).count(status) for status in STATUS_ICON}
    lines = [f"{len(statuses)} subject(s) with {what}"]
    lines += [f"{STATUS_ICON[status][0]} {n} {status}" for status, n in counts.items() if n]
    if output_root is None:
        lines.append("Choose an output folder.")
    return "\n".join(lines)


def suggested_plan(probe: DockerProbe, ticked: int) -> ParallelPlan:
    return suggest_plan(probe.cpus or os.cpu_count(), probe.gb, ticked)


def suggest_note(probe: DockerProbe, ticked: int) -> str:
    """Why the suggestion runs fewer subjects at once than Docker would fit, if so."""
    if ticked and suggest_plan(probe.cpus or os.cpu_count(), probe.gb).parallel > ticked:
        return f" (only {ticked} subject(s) ticked)"
    return ""


def memory_text(
    plan: ParallelPlan, probe: DockerProbe, host_gb: float | None, ticked: int
) -> tuple[str, str]:
    """How the plan fits this computer and Docker: (text, colour)."""
    problem = memory_problem(plan.mem_gb, probe.gb, host_gb)
    warning = parallel_warning(plan, probe.gb, probe.cpus)
    host = f"{host_gb:.1f} GB" if host_gb is not None else "unknown"
    cpus = f", {probe.cpus} CPUs" if probe.cpus else ""
    if problem is not None:
        text, color = f"⚠ {problem}", ERROR_COLOR
    elif probe.gb is None:
        text, color = f"This computer: {host} RAM. Docker's limit couldn't be read.", UNKNOWN_COLOR
    elif warning is not None:
        text, color = f"⚠ {warning}", WARNING_COLOR
    else:
        text = f"✓ Docker may use {probe.gb:.1f} GB of this computer's {host} RAM{cpus}."
        color = OK_COLOR
    if plan.parallel > 1:
        text += (
            f"\nIn use at once: {plan.parallel * plan.n_cpus} CPUs, "
            f"{plan.parallel * plan.mem_gb:g} GB."
        )
    suggested = suggested_plan(probe, ticked)
    if suggested != plan and probe.gb is not None:
        text += (
            f"\nSuggested here: {suggested.parallel} at once{suggest_note(probe, ticked)} x "
            f"{suggested.n_cpus} CPUs, {suggested.mem_gb:g} GB each (click 'Suggest')."
        )
    return text, color


def scratch_question(subjects: list[str]) -> str:
    return (
        f"This deletes the C-PAC working and output folders of {len(subjects)} "
        "subject(s) before running them again (logs are kept). Continue?"
    )


def overload_question(plan: ParallelPlan, probe: DockerProbe, ticked: int) -> str:
    """What to ask when the plan is more than Docker allows ('' when it isn't)."""
    warning = parallel_warning(plan, probe.gb, probe.cpus)
    if warning is None or plan.parallel <= 1:
        return ""
    suggested = suggested_plan(probe, ticked)
    return (
        f"{warning}\n\nSuggested for this computer: {suggested.parallel} at once x "
        f"{suggested.n_cpus} CPUs, {suggested.mem_gb:g} GB ('Suggest' fills it in). "
        "Run anyway?"
    )


def stop_now_question(running: int) -> str:
    return (
        f"Stop the {running or 'running'} C-PAC container(s) now and start no "
        "new subjects? This frees the CPUs and memory. Finished steps are kept: running "
        "the batch again later resumes each subject where it stopped."
    )


def start_text(run: RunSettings) -> str:
    at_once = f", {run.parallel} at once" if run.parallel > 1 else ""
    return (
        f"Preprocessing {len(run.subjects)} subject(s), {task_names(run.tasks)}{at_once}, "
        f"{run.n_cpus} CPU(s), {run.mem_gb:g} GB each..."
    )


def done_lines(result: RunSummary) -> list[tuple[str, bool]]:
    """(line, is an error) for the activity log once the batch ended."""
    stopped = " (stopped early)" if result.stopped else ""
    lines = [
        (
            f"Done{stopped}: {len(result.done)} preprocessed, {len(result.skipped)} "
            f"skipped (already done), {len(result.failed)} failed.",
            False,
        )
    ]
    if result.failed:  # each cause was logged as it happened
        failed = ", ".join(f"sub-{subject}" for subject, _why in result.failed)
        lines.append((f"Failed: {failed}", True))
    return lines


# ---- one subject's outputs ----------------------------------------------------------------------


@dataclass
class Problem:
    """A box above a subject's outputs."""

    title: str
    text: str
    color: str
    listing: list[str] = field(default_factory=list)


def failure_problem(output_root: Path, subject: str, stopped: bool) -> Problem:
    """Why this subject's last C-PAC run failed (or that it was stopped), read back from its
    saved log."""
    cause = explain_failure(subject_console_log(output_root, subject), output_root)
    return Problem(
        "Stopped before it finished" if stopped else "Why it failed",
        cause or "No known cause recognised; see the C-PAC Log tab.",
        WARNING_COLOR if stopped else ERROR_COLOR,
    )


def partial_problem(to_do: list[str]) -> Problem:
    """Which tasks an 'All tasks' run would still preprocess for this subject."""
    return Problem(
        "Not all tasks preprocessed",
        f"Still to do: {task_names(to_do)}. Run with these tasks ticked and Run mode 'Skip done, "
        "resume the rest': finished steps are reused.",
        WARNING_COLOR,
    )


def renumbered_problem(changes: list[SessionChange]) -> Problem:
    """The subject's BIDS sessions were renumbered after it was preprocessed."""
    return Problem(
        "BIDS sessions renumbered since preprocessing",
        "An earlier session was added to BIDS, so this subject's sessions have new "
        "numbers (listed here). Its output still carries the old ones. Either run it "
        "again (its old results are cleared first and every session is preprocessed "
        "again), or rename its output to the new numbers.",
        WARNING_COLOR,
        [str(change) for change in changes],
    )


def rename_question(subject: str, changes: list[SessionChange]) -> str:
    listing = "\n".join(f"  {change}" for change in changes)
    return (
        f"Rename sub-{subject}'s C-PAC output to the new session numbers?\n{listing}\n\n"
        "Its C-PAC working data is cleared too (Docker must be running)."
    )


def missing_problem(
    bids_root: Path, output_root: Path, subject: str, tasks: list[str]
) -> Problem | None:
    """The outputs C-PAC said it would write (expectedOutputs.yml) but didn't, per session."""
    sessions = bold_sessions(bids_root, subject, tasks)
    gaps = missing_outputs(output_root, subject, sessions, tasks)
    lines = [f"ses-{session}  {name}" for session, names in gaps.items() for name in names]
    if not lines:
        return None
    n_missing = len(lines)
    if n_missing > MAX_MISSING_SHOWN:
        hidden = n_missing - MAX_MISSING_SHOWN
        lines = [*lines[:MAX_MISSING_SHOWN], f"... and {hidden} more"]
    return Problem(
        f"Not complete: {n_missing} expected output(s) missing",
        "Run it again with Run mode 'Skip done, resume the rest' (same CPUs): finished "
        "steps are reused, so only what's missing is redone.",
        WARNING_COLOR,
        lines,
    )


def motion_rows(tables: list[Path]) -> tuple[list[str], list[tuple[str, dict[str, str]]]]:
    """(the TSVs' own columns, one (run, values) per run)"""
    columns: list[str] = []
    rows: list[tuple[str, dict[str, str]]] = []
    for tsv in tables:
        entities = bids_entities(tsv.name)
        run = f"ses-{entities.get('ses', '?')} {entities.get('task', '')}".strip()
        try:
            header, values = read_tsv(tsv)
        except (OSError, UnicodeDecodeError) as e:
            header, values = ["Error"], [[f"Couldn't read {tsv.name}: {e}"]]
        columns += [name for name in header if name not in columns]
        rows += [(run, dict(zip(header, row, strict=False))) for row in values]
    return columns, rows


def log_text(output_root: Path, subject: str) -> str:
    """The end of the subject's C-PAC console log, or why there is none."""
    console_log = subject_console_log(output_root, subject)
    try:
        return read_log_tail(console_log)
    except FileNotFoundError:
        return "No C-PAC run logged for this subject yet."
    except OSError as e:
        return f"Couldn't read {console_log}: {e}"
