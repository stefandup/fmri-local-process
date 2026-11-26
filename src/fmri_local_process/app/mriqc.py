"""Scan quality (MRIQC): the batch job, what is ticked, the Docker limits and the texts."""

import threading
from pathlib import Path
from typing import Protocol

from fmri_local_process.app import figures
from fmri_local_process.app.colors import (
    OK_COLOR,
    RUNNING_COLOR,
    UNKNOWN_COLOR,
    WARNING_COLOR,
)
from fmri_local_process.app.settings import Settings, saved_number
from fmri_local_process.app.state import ToolboxState
from fmri_local_process.processing import mriqc as mq
from fmri_local_process.processing import mriqc_views
from fmri_local_process.processing.cpac_docker import docker_resources, finished_node

OUTLIER_BACKGROUND = "#f5b7b1"
RATING_COLORS = {1: "#f1948a", 2: "#f9e79f", 3: "#abebc6"}  # black text reads on these
QC_COLUMN, FLAGS_COLUMN, FIRST_METRIC_COLUMN = 1, 2, 3
STALE_BACKGROUND = "#f9e79f"
RUNNING, FAILED = "running", "failed"
STATUS_COLOR = {
    RUNNING: RUNNING_COLOR,
    mq.DONE: OK_COLOR,
    mq.STALE: WARNING_COLOR,
    mq.PARTIAL: WARNING_COLOR,
    mq.NOT_RUN: UNKNOWN_COLOR,
}
MODALITY_TEXT = {"bold": "BOLD (functional)", "T1w": "T1w (anatomical)"}
T1W_SCAN = "T1w"  # the scan list's keys: this, and 'task-<label>' per BOLD task
UNTICKED_SCANS_KEY = "mriqc_unticked_scans"
# The Docker line for each probe result: (running, why not, warning); '' (unknown) is absent.
DOCKER_LINE = {
    mq.DOCKER_MISSING: (False, "it isn't installed (Docker Desktop).", ""),
    mq.DOCKER_STOPPED: (False, "start it to run MRIQC.", ""),
    mq.IMAGE_MISSING: (True, "", "Docker is running, but the MRIQC image isn't downloaded yet."),
    mq.READY: (True, "", ""),
}
HELP_TEXT = (
    "<b>MRIQC</b> measures the quality of each raw BOLD and T1w scan (no preprocessing "
    "needed), so poor scans are found before hours of C-PAC.<br><br>"
    "Choose an <b>MRIQC output</b> folder, tick subjects and <b>Run MRIQC</b>. Each subject "
    "takes about 15-40 minutes. Done subjects are skipped; a subject whose BIDS files changed "
    "since (trimmed, reconverted) is <b>stale</b> and is run again.<br><br>"
    "The table groups the runs by subject and session. A <span style='background-color:"
    f"{OUTLIER_BACKGROUND}; color:black'>&nbsp;highlighted&nbsp;</span> value is an outlier "
    "among the same kind of scan (same task) in this dataset: more than 1.5 x the "
    "interquartile range beyond the quartiles, on the worse side (needs 5 or more runs). Hover "
    "a column header for what the metric means, or <b>Metric formulas...</b> (above the "
    "table) for how each is calculated, typical values and references.<br><br>"
    "Select a run to see the scan below (drawn from the BIDS file), then rate it: <b>1 bad "
    "(discard)</b>, <b>2 unsure</b> or <b>3 keep</b>, with a comment if useful. Ratings are "
    "saved at once, in toolbox_qc_ratings.tsv in the MRIQC output folder."
    "<br><br>Nothing leaves this computer: MRIQC's upload of anonymous metrics to its web "
    "database and its usage tracking are both switched off."
)


# ---- jobs ---------------------------------------------------------------------------------------


def probe_docker() -> tuple[str, str, int | None, float | None]:
    """Whether Docker runs and has the MRIQC image: (state, message, CPUs, GB). Slow when
    Docker is starting: call it off the UI thread."""
    state, message, docker = mq.docker_status()
    mem_gb, cpus = docker_resources(docker) if docker is not None else (None, None)
    return state, message, cpus, mem_gb


class MriqcListener(Protocol):
    """What the job tells the UI while it runs (called on the job's thread)."""

    def on_message(self, text: str, is_error: bool, /) -> None: ...
    def on_subject_started(self, subject: str, /) -> None: ...
    def on_step(self, subject: str, steps: int, node: str, /) -> None: ...
    def on_subject_done(self, subject: str, failure: str, /) -> None: ...  # "" when done
    def on_stage(self, text: str, /) -> None: ...  # what runs between subjects
    def on_all_done(self, summary: str, /) -> None: ...


class MriqcJob:
    """MRIQC for each subject in turn, then the group step."""

    def __init__(
        self,
        docker: Path,
        bids_root: Path,
        output_root: Path,
        subjects: list[str],
        n_procs: int,
        mem_gb: float,
        selection: mq.Selection,
        listener: MriqcListener,
    ) -> None:
        self.docker, self.bids_root, self.output_root = docker, bids_root, output_root
        self.subjects, self.n_procs, self.mem_gb = subjects, n_procs, mem_gb
        self.selection, self.listener = selection, listener
        self.stop_requested = False
        self.current: str | None = None
        self.steps = 0

    def run(self) -> None:
        done, failed = [], []
        for number, subject in enumerate(self.subjects, start=1):
            if self.stop_requested:
                break
            self.current, self.steps = subject, 0
            self.listener.on_message(
                f"sub-{subject} ({number} of {len(self.subjects)}): started", False
            )
            self.listener.on_subject_started(subject)
            try:
                failure = mq.run_subject(
                    self.docker,
                    self.bids_root,
                    self.output_root,
                    subject,
                    self.n_procs,
                    self.mem_gb,
                    self.on_line,
                    selection=self.selection,
                )
            except OSError as e:
                failure = str(e)
            self.current = None
            if failure is not None and self.stop_requested:
                failure = "stopped by you; running it again starts it afresh"
            if failure is None:
                done.append(subject)
                self.listener.on_message(f"sub-{subject}: done", False)
            else:
                failed.append(subject)
                self.listener.on_message(f"sub-{subject}: FAILED: {failure}", True)
            self.listener.on_subject_done(subject, failure or "")
        if done and not self.stop_requested:
            self.listener.on_message("Group step: summary tables and group reports...", False)
            self.listener.on_stage("Group step: summary tables and group reports...")
            log_path = self.output_root / mq.TOOLBOX_LOGS / "mriqc_group.txt"
            command = mq.build_group_command(self.docker, self.bids_root, self.output_root)
            try:
                code = mq.run_logged(command, log_path)
            except OSError as e:
                code, log_path = -1, Path(str(e))
            if code != 0:
                self.listener.on_message(
                    f"Group step FAILED: {mq.explain_failure(log_path, code)}", True
                )
        stopped = " (stopped)" if self.stop_requested else ""
        self.listener.on_all_done(
            f"MRIQC finished{stopped}: {len(done)} done, {len(failed)} failed."
        )

    def on_line(self, line: str) -> None:
        """MRIQC is a nipype pipeline like C-PAC: count its finished steps the same way."""
        node = finished_node(line)
        if node is not None and self.current is not None:
            self.steps += 1
            self.listener.on_step(self.current, self.steps, node.rsplit(".", 1)[-1])

    def stop(self) -> None:
        """Start no more subjects and stop the running container (in the background: docker
        takes up to ~10 s)."""
        self.stop_requested = True
        if self.current is not None:
            name = mq.container_name(self.current)
            threading.Thread(
                target=mq.stop_container, args=(self.docker, name), daemon=True
            ).start()

    def stop_and_wait(self) -> None:
        """Closing: stop the running container before leaving."""
        self.stop_requested = True
        if self.current is not None:
            mq.stop_container(self.docker, mq.container_name(self.current))


def view_png(path: Path, modality: str) -> tuple[bytes | None, str]:
    """One scan's view as a PNG, or why not (reading a whole BOLD series takes a second or
    more)."""
    try:
        return figures.png_bytes(mriqc_views.run_figure(path, modality)), ""
    except (mriqc_views.ViewError, MemoryError) as e:
        return None, str(e)


# ---- which scans and subjects -------------------------------------------------------------------


def scan_keys(bids_root: Path | None) -> list[str]:
    """What can be ticked: the T1w and each BOLD task."""
    if bids_root is None or not bids_root.is_dir():
        return []
    keys = [T1W_SCAN] if mq.has_t1w(bids_root) else []
    return keys + [f"task-{task}" for task in mq.bids_tasks(bids_root)]


def scan_text(key: str) -> str:
    return "T1w (anatomical)" if key == T1W_SCAN else f"{key} (BOLD)"


def make_selection(keys: list[str], unticked: set[str]) -> mq.Selection:
    """The ticked scans; every BOLD task ticked counts as all tasks (new ones too)."""
    tasks = [key for key in keys if key.startswith("task-")]
    ticked = [key.removeprefix("task-") for key in tasks if key not in unticked]
    return mq.Selection(
        t1w=T1W_SCAN not in unticked,
        tasks=None if len(ticked) == len(tasks) else tuple(ticked),
    )


def saved_unticked(settings: Settings) -> set[str]:
    return set(filter(None, str(settings.value(UNTICKED_SCANS_KEY, "") or "").split(",")))


def save_unticked(settings: Settings, unticked: set[str]) -> None:
    settings.setValue(UNTICKED_SCANS_KEY, ",".join(sorted(unticked)))


def subjects_and_states(
    bids_root: Path | None, output_root: Path | None, selection: mq.Selection
) -> tuple[list[str], dict[str, str]]:
    if bids_root is None or not bids_root.is_dir():
        return [], {}
    subjects = mq.bids_subjects(bids_root, selection)
    return subjects, {s: mq.subject_state(bids_root, output_root, s, selection) for s in subjects}


# A subject's MRIQC run takes about the same number of steps per scan each time, so the
# bar's estimate is the last complete run's steps per scan x this subject's scans.


def subject_scans(bids_root: Path | None, subject: str, selection: mq.Selection) -> int:
    return len(mq.bids_scans(bids_root, subject, selection)) if bids_root else 0


def steps_estimate(settings: Settings, scans: int) -> int | None:
    return round(saved_number(settings, "mriqc_steps_per_scan", 0) * scans) or None


# ---- texts --------------------------------------------------------------------------------------


def notices(
    state: ToolboxState,
    keys: list[str],
    nothing_ticked: bool,
    runs: list[mq.RunIqms],
    unreadable: list[str],
    modality: str,
    results_only: bool,
    docker_message: str,
) -> list[str]:
    """What is missing, most basic first."""
    bids_root, output_root = state.folder("bids"), state.folder("mriqc")
    messages = []
    if bids_root is None:
        messages.append("No BIDS folder chosen: choose it on the Setup & Convert to BIDS tab.")
    elif not keys:
        messages.append(
            f"No BOLD or T1w scans in {bids_root}: convert the DICOMs on the Setup & "
            "Convert to BIDS tab first."
        )
    elif nothing_ticked:
        messages.append("No scans ticked: tick the scans MRIQC should check (left).")
    if output_root is None:
        messages.append(
            "No MRIQC output folder chosen: Browse for one above; MRIQC writes its reports there."
        )
    elif not runs and not mq.has_results(output_root):
        todo = "" if results_only else " yet: tick subjects and Run MRIQC"
        messages.append(f"No MRIQC results in {output_root}{todo}.")
    elif not runs:
        messages.append(f"No MRIQC results for {MODALITY_TEXT[modality]} scans in {output_root}.")
    if unreadable:
        messages.append(
            f"{len(unreadable)} MRIQC result file(s) couldn't be read: " + "; ".join(unreadable[:3])
        )
    if docker_message and not results_only:
        messages.append(docker_message)
    return messages


def run_status(reason: str, docker_state: str, ticked: int, to_run: int) -> str:
    """The line under the Run button while nothing runs."""
    if reason:
        return f"Locked: {reason}."
    if docker_state == "":
        return "Checking Docker..."
    if ticked and not to_run:
        return "Every ticked subject is done (tick 'Redo' to rerun)."
    return f"{to_run} subject(s) to run."


def limits_text(docker_cpus: int | None, docker_gb: float | None, chosen: tuple[int, float]) -> str:
    if docker_cpus is None and docker_gb is None:
        return "Docker's CPU and memory limits not known yet."
    cpus = f"{docker_cpus} CPUs" if docker_cpus else "? CPUs"
    memory = f"{docker_gb:.1f} GB" if docker_gb else "? GB"
    suggested = mq.suggest_resources(docker_cpus, docker_gb)
    note = "" if chosen == suggested else f" Suggested: {suggested[0]} CPUs, {suggested[1]:g} GB."
    return f"Docker allows {cpus}, {memory}.{note}"


def flags_text(run: mq.RunIqms, flagged: set[str]) -> str:
    note = f"{len(flagged)} outlier(s)" if flagged else ""
    return ", ".join(filter(None, [note, "stale" if run.state == mq.STALE else ""]))


def metric_cells(run: mq.RunIqms, modality: str) -> list[tuple[str, str]]:
    """(metric key, value as shown) per column."""
    values = [(m.key, run.values.get(m.key)) for m in mq.METRICS[modality]]
    return [(key, "" if value is None else f"{value:.3g}") for key, value in values]


def metric_help(metric: mq.Metric) -> str:
    return metric.help + (f" ({metric.better} is better)" if metric.better else "")


def rating_text(rating: mq.QcRating | None) -> str:
    if rating is None:
        return ""
    return "comment only" if rating.comment and not rating.rating else rating.text


def rating_details(rating: mq.QcRating) -> str:
    who = ", ".join(filter(None, [rating.rater, rating.date.replace("T", " ")]))
    return "\n".join(filter(None, [rating.comment, f"({who})" if who else ""]))


def rating_changed(old: mq.QcRating | None, rating: int | None, comment: str) -> bool:
    if old is None:
        return rating is not None or bool(comment)
    return (old.rating, old.comment) != (rating, comment)
