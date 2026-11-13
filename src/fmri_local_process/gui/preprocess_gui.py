"""fmri_preprocess_gui: run and follow C-PAC preprocessing of a BIDS folder (PySide6).

Same layout as vr_lab_toolbox's process-results tools: BIDS Folder / Output Folder /
Summary panels and the Activity Log on top, subjects on the left, the selected subject's
outputs and C-PAC log on the right. The run itself is the same per-subject call the
fmri_preprocess CLI makes (`preprocess_subject`), on a worker thread so the window stays
responsive through hours of C-PAC.
"""

import html
import logging
import os
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from nibabel.filebasedimages import ImageFileError
from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.gui.image_view import (
    HelpToggle,
    PreviewPicker,
    figure_to_pixmap,
    load_figure,
)
from fmri_local_process.gui.output_help import MOTION_HELP, qc_figure_help, slice_help
from fmri_local_process.gui.qt_common import (
    ERROR_COLOR,
    HOME_BASE_ACCENT_COLOR,
    HOME_BASE_NAME_EXTRA_POINT_INCREASE,
    OK_COLOR,
    PROCESS_ACCENT_COLOR,
    PROCESS_ACCENT_HOVER_COLOR,
    RUNNING_COLOR,
    SETTINGS_ORGANIZATION,
    UNKNOWN_COLOR,
    WARNING_COLOR,
    accent_group_box_stylesheet,
    default_browse_dir,
    paths_conflict,
    primary_action_stylesheet,
    reveal,
    set_path_display,
    style_name_label,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.processing.brain_slices import SlicePreview, render_slices, slice_previews
from fmri_local_process.processing.cpac_docker import (
    CPAC_IMAGE,
    MIN_CPUS_PER_SUBJECT,
    MIN_MEM_GB,
    START_GAP_S,
    SUGGESTED_MEM_GB,
    DockerUnavailableError,
    ParallelPlan,
    StartGate,
    bids_subjects,
    bids_tasks,
    bold_sessions,
    clear_subject_results,
    docker_desktop,
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
    preprocessed_bolds,
    run_cpac,
    stop_containers,
    subject_console_log,
    subject_tasks_in,
    suggest_plan,
    was_stopped,
)
from fmri_local_process.processing.cpac_outputs import (
    QcFigure,
    bids_entities,
    motion_summaries,
    qc_figures,
    read_tsv,
)

log = logging.getLogger(__name__)

SETTINGS_APP = "FmriPreprocess"
TASK_LIST_HEIGHT = 96  # ~4 rows; more tasks scroll
SUMMARY_MIN_WIDTH = 200
LOG_TAIL_LINES = 400
MAX_MISSING_SHOWN = 12
DOCKER_RECHECK_MS = 30_000
SLICE_CACHE_SIZE = 40  # rendered slice previews kept (~3 MB each)
STATUS_ICON = {
    "done": ("✓", OK_COLOR),
    "partial": ("◐", WARNING_COLOR),
    "failed": ("✗", ERROR_COLOR),
    "stopped": ("■", WARNING_COLOR),
    "running": ("▶", RUNNING_COLOR),
    "not run": ("○", UNKNOWN_COLOR),
}
SPINNER = "◐◓◑◒"
STATUS_LEGEND = (
    "✓ every session has a preprocessed BOLD (of each ticked task) and every output C-PAC "
    "expected\n"
    "◐ (several tasks ticked) some are preprocessed, others not yet\n"
    "✗ C-PAC ran (a log exists) but its output is incomplete\n"
    "■ stopped by you before it finished; running it again resumes it\n"
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


@dataclass
class RunningSubject:
    started_at: float
    steps: int = 0
    last_step: str = ""


@dataclass
class RunSummary:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False


class PreprocessThread(QThread):
    """Runs the batch: Docker check, one-time image pull, then one C-PAC container per
    subject, run.parallel of them at a time."""

    message = Signal(str, bool)  # text, is_error
    subject_started = Signal(int, int, str)  # index, total, subject
    step = Signal(str, str, int)  # subject, finished node, steps done
    subject_finished = Signal(str, int, bool)  # subject, steps done, preprocessed by this run
    batch_done = Signal(object)  # RunSummary, or an error message

    def __init__(self, run: RunSettings) -> None:
        super().__init__()
        self.run_settings = run
        self.stop_requested = False
        self.docker: Path | None = None
        # Stop now: `docker stop` the running containers. One that is still starting (no
        # container to stop yet) is stopped at its first console line instead.
        self.stop_now_requested = False
        self.lock = threading.Lock()
        self.active: set[str] = set()  # subjects whose container is (about to be) running
        self.stopping: set[str] = set()  # ... and of those, the ones docker was told to stop

    def run(self) -> None:
        run = self.run_settings
        try:
            docker = find_docker()
        except DockerUnavailableError as e:
            self.batch_done.emit(str(e))
            return
        self.docker = docker
        problem = memory_problem(run.mem_gb, docker_memory_gb(docker), host_memory_gb())
        if problem is not None:
            self.batch_done.emit(problem)
            return
        if not image_present(docker, run.image):
            self.message.emit(
                f"Pulling C-PAC image {run.image} (~15 GB, one-time; can take 30+ min)...", False
            )
            pull_log = run.output_root / "log" / "docker_pull.txt"
            pull_cmd = [str(docker), "pull", run.image]
            if run_cpac(pull_cmd, pull_log, self.relay_pull_line) != 0:
                self.batch_done.emit(f"docker pull {run.image} failed; see {pull_log}")
                return

        summary = RunSummary()
        gate = StartGate(START_GAP_S if run.parallel > 1 else 0.0)
        # Each subject runs on a pool thread; Qt queues the signals to the GUI thread.
        with ThreadPoolExecutor(max_workers=run.parallel) as pool:
            for index, subject in enumerate(run.subjects):
                pool.submit(self.run_subject, docker, index, subject, gate, summary)
        self.batch_done.emit(summary)

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
            self.subject_finished.emit(subject, 0, False)
            return
        gate.wait()
        with self.lock:  # under the lock, so stop_now() can't miss a starting subject
            if self.stop_requested:  # pressed while waiting for a start slot
                summary.stopped = True
                return
            self.active.add(subject)
        self.subject_started.emit(index, len(run.subjects), subject)
        steps_done = 0

        def on_line(line: str) -> None:
            nonlocal steps_done
            if self.stop_now_requested and subject not in self.stopping:
                self.stop_container(subject)
            node = finished_node(line)
            if node is not None:
                steps_done += 1
                self.step.emit(subject, node, steps_done)

        try:
            if run.mode == "scratch":
                removed = clear_subject_results(run.output_root, subject)
                self.message.emit(
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
            )
        except Exception as e:  # noqa: BLE001 -- one subject's crash mustn't end the batch
            log.exception("sub-%s crashed", subject)
            failure = f"{type(e).__name__}: {e}"
        with self.lock:
            self.active.discard(subject)
            stopped = subject in self.stopping
        if failure is None:
            summary.done.append(subject)
            self.message.emit(f"sub-{subject}: preprocessed ({steps_done} steps)", False)
        elif stopped:
            mark_stopped(subject_console_log(run.output_root, subject))
            summary.stopped = True
            self.message.emit(
                f"sub-{subject}: stopped after {steps_done} steps; running it again resumes it",
                False,
            )
        else:
            summary.failed.append((subject, failure))
            self.message.emit(f"sub-{subject} FAILED: {failure}", True)
        self.subject_finished.emit(subject, steps_done, failure is None)

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
            self.message.emit(f"sub-{subject}: couldn't stop its container: {error}", True)

    def stop_now(self) -> list[str]:
        """Start no new subjects and stop the running containers, freeing their CPUs and
        memory; returns the subjects stopped (blocks on docker: call it off the GUI thread)."""
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
            self.message.emit(f"docker pull: {text}", False)


class DockerControlThread(QThread):
    """Starts / restarts Docker Desktop off the GUI thread (takes up to a minute or two)."""

    done = Signal(str, str)  # action, error ("" when it worked)

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action

    def run(self) -> None:
        self.done.emit(self.action, docker_desktop(self.action) or "")


class MemoryProbeThread(QThread):
    """Asks Docker for its memory and CPU limits off the GUI thread (slow when Docker is
    starting)."""

    done = Signal(object, object, str)  # Docker's GB or None, its CPUs or None, why unknown

    def run(self) -> None:
        try:
            docker = find_docker()
        except DockerUnavailableError as e:
            self.done.emit(None, None, str(e))
            return
        self.done.emit(*docker_resources(docker), "")


def read_log_tail(path: Path, n_lines: int = LOG_TAIL_LINES) -> str:
    """Last n_lines of a (possibly multi-MB) C-PAC console log."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    skipped = len(lines) - n_lines
    head = (
        [f"... {skipped} earlier lines, open the file for all of it ...", ""] if skipped > 0 else []
    )
    return "\n".join(head + lines[-n_lines:])


class PreprocessWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("fMRI Preprocess (C-PAC)")
        self.resize(1300, 850)
        self.settings = QSettings(SETTINGS_ORGANIZATION, SETTINGS_APP)
        self.bids_folder: Path | None = None
        self.output_folder: Path | None = None
        self.subjects: list[str] = []
        self.statuses: dict[str, str] = {}
        self.running: dict[str, RunningSubject] = {}  # in start order
        self.worker: PreprocessThread | None = None
        self.live_line: str | None = None
        self.host_gb = host_memory_gb()
        self.docker_gb: float | None = None
        self.docker_cpus: int | None = None
        self.docker_error = ""
        self.memory_probed = False
        self.docker_control: DockerControlThread | None = None
        # Untick-list rather than tick-list, so newly found subjects start ticked.
        self.unticked: set[str] = set()
        self.unticked_tasks: set[str] = set()

        self._build_ui()
        self._restore_settings()

    # ---- layout -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)

        top_row = QHBoxLayout()
        folders_grid = QGridLayout()
        folders_grid.setSpacing(8)
        folders_grid.addWidget(self._build_bids_group(), 0, 0)
        folders_grid.addWidget(self._build_output_group(), 0, 1)
        folders_grid.addWidget(self._build_summary_group(), 0, 2)
        top_row.addLayout(folders_grid, 1)

        activity_group = QGroupBox("Activity Log")
        activity_layout = QVBoxLayout(activity_group)
        self.activity_text = QTextEdit()
        self.activity_text.setReadOnly(True)
        self.activity_text.setFont(QFont("Courier New"))
        self.activity_text.setPlaceholderText(
            'Click "Preprocess" to run C-PAC; progress and results appear here.'
        )
        activity_layout.addWidget(self.activity_text)
        top_row.addWidget(activity_group, 1)
        root_layout.addLayout(top_row)

        root_layout.addWidget(self._build_run_group())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(
            QLabel("Tick the subjects to preprocess; click a row to preview its outputs and log.")
        )
        tick_row = QHBoxLayout()
        self.tick_all_button = QPushButton("Tick all")
        self.tick_all_button.clicked.connect(partial(self._tick_all, True))
        tick_row.addWidget(self.tick_all_button)
        self.tick_none_button = QPushButton("Tick none")
        self.tick_none_button.clicked.connect(partial(self._tick_all, False))
        tick_row.addWidget(self.tick_none_button)
        tick_row.addStretch(1)
        left_layout.addLayout(tick_row)
        self.subject_table = QTableWidget(0, 3)
        self.subject_table.setHorizontalHeaderLabels(["Subject", "Sessions", "Status"])
        status_header = QTableWidgetItem("Status")
        status_header.setToolTip(STATUS_LEGEND)
        self.subject_table.setHorizontalHeaderItem(2, status_header)
        self.subject_table.verticalHeader().setVisible(False)
        self.subject_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.subject_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.subject_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.subject_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.subject_table.setColumnWidth(1, 70)
        self.subject_table.setColumnWidth(2, 55)
        self.subject_table.itemSelectionChanged.connect(self._render_detail)
        self.subject_table.itemChanged.connect(self._on_tick_changed)
        left_layout.addWidget(self.subject_table, 1)
        splitter.addWidget(left)

        self.detail_tabs = QTabWidget()
        self.outputs_widget = QWidget()
        self.outputs_layout = QVBoxLayout(self.outputs_widget)
        self.detail_tabs.addTab(self.outputs_widget, "Outputs")
        self.figure_picker = PreviewPicker(
            load_figure,
            lambda figure: qc_figure_help(figure.desc, figure.reg),
            "No QC figures for this subject yet.",
            "QC figures C-PAC drew for this subject (of the chosen task).",
        )
        self.detail_tabs.addTab(self.figure_picker, "QC Figures")
        self.slice_cache: dict[tuple[SlicePreview, float], QPixmap] = {}
        self.slice_picker = PreviewPicker(
            self._load_slices,
            lambda preview: slice_help(preview.kind),
            "No brain images for this subject yet.",
            wrap_tooltip(
                "Slices through C-PAC's T1w and BOLD outputs with mask outlines: check "
                "skull stripping, tissue masks, BOLD-to-T1w coregistration and the "
                "warp to the template."
            ),
        )
        self.detail_tabs.addTab(self.slice_picker, "Brain Slices")
        self.motion_table = QTableWidget(0, 0)
        self.motion_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.motion_table.verticalHeader().setVisible(False)
        self.motion_table.setToolTip(
            wrap_tooltip("C-PAC's per-run motion summary (desc-summary_motion.tsv).")
        )
        self.motion_tab = QWidget()
        motion_layout = QVBoxLayout(self.motion_tab)
        self.motion_help = HelpToggle()
        self.motion_help.set_text(MOTION_HELP)
        help_row = QHBoxLayout()
        help_row.addStretch(1)
        help_row.addWidget(self.motion_help.button)
        motion_layout.addLayout(help_row)
        motion_layout.addWidget(self.motion_help.box)
        motion_layout.addWidget(self.motion_table, 1)
        self.detail_tabs.addTab(self.motion_tab, "Motion")
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Courier New"))
        self.log_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.detail_tabs.addTab(self.log_text, "C-PAC Log")
        splitter.addWidget(self.detail_tabs)
        splitter.setSizes([380, 920])
        root_layout.addWidget(splitter, 1)

    def _build_bids_group(self) -> QGroupBox:
        group = QGroupBox("BIDS Folder")
        layout = QVBoxLayout(group)
        self.bids_path_label = QLabel("")
        style_secondary_label(self.bids_path_label)
        layout.addWidget(self.bids_path_label)
        self.bids_name_label = QLabel("No BIDS folder selected")
        style_name_label(self.bids_name_label)
        layout.addWidget(self.bids_name_label)

        buttons = QHBoxLayout()
        browse = QPushButton("Browse...")
        browse.setToolTip("Pick the crosschecked BIDS folder to preprocess.")
        browse.clicked.connect(self._on_browse_bids)
        buttons.addWidget(browse)
        self.bids_reveal_button = QPushButton("Reveal")
        self.bids_reveal_button.setEnabled(False)
        self.bids_reveal_button.clicked.connect(partial(self._reveal, "bids"))
        buttons.addWidget(self.bids_reveal_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        form = QFormLayout()
        self.task_list = QListWidget()
        self.task_list.setMaximumHeight(TASK_LIST_HEIGHT)
        self.task_list.setToolTip(
            wrap_tooltip(
                "Tick the BOLD tasks to preprocess (scroll for more). All ticked: every BOLD "
                "run. Each subject still runs once, with all its ticked tasks together."
            )
        )
        self.task_list.itemChanged.connect(self._on_task_ticked)
        task_buttons = QVBoxLayout()
        for text, ticked in (("All", True), ("None", False)):
            button = QPushButton(text)
            button.clicked.connect(partial(self._tick_all_tasks, ticked))
            task_buttons.addWidget(button)
        task_buttons.addStretch(1)
        task_row = QHBoxLayout()
        task_row.addWidget(self.task_list, 1)
        task_row.addLayout(task_buttons)
        form.addRow("Tasks:", task_row)
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, max(1, (os.cpu_count() or 1) // 2))
        self.parallel_spin.setSuffix(" at once")
        self.parallel_spin.setToolTip(
            wrap_tooltip(
                "How many subjects run side by side, each in its own C-PAC container with "
                "the CPUs and Memory below (as the philani CHPC jobs did). Many C-PAC steps "
                "use one core, so several subjects with a few cores each finish a batch "
                "sooner than one subject with all of them. The totals must fit Docker's "
                "limits: 'Suggest' picks values that do."
            )
        )
        self.parallel_spin.valueChanged.connect(self._update_memory_label)
        parallel_row = QHBoxLayout()
        parallel_row.addWidget(self.parallel_spin, 1)
        self.suggest_button = QPushButton("Suggest")
        self.suggest_button.setToolTip(
            wrap_tooltip(
                f"Fill in Subjects, CPUs and Memory from Docker's limits: about "
                f"{SUGGESTED_MEM_GB:g} GB and at least {MIN_CPUS_PER_SUBJECT} CPUs per "
                "subject, at most as many subjects as are ticked."
            )
        )
        self.suggest_button.clicked.connect(self._on_suggest)
        parallel_row.addWidget(self.suggest_button)
        form.addRow("Subjects:", parallel_row)
        self.cpus_spin = QSpinBox()
        self.cpus_spin.setRange(1, os.cpu_count() or 1)
        self.cpus_spin.setToolTip("CPU cores C-PAC may use per subject.")
        self.cpus_spin.valueChanged.connect(self._update_memory_label)
        form.addRow("CPUs:", self.cpus_spin)
        self.mem_spin = QDoubleSpinBox()
        self.mem_spin.setRange(MIN_MEM_GB, 512.0)
        self.mem_spin.setSingleStep(1.0)
        self.mem_spin.setDecimals(0)
        self.mem_spin.setSuffix(" GB")
        self.mem_spin.setToolTip(
            wrap_tooltip(
                f"RAM C-PAC may use per subject. At least {MIN_MEM_GB:g} GB: below that "
                "C-PAC refuses to start. Docker Desktop's own memory limit (Settings > "
                "Resources) must allow at least this much."
            )
        )
        self.mem_spin.valueChanged.connect(self._update_memory_label)
        form.addRow("Memory:", self.mem_spin)
        layout.addLayout(form)
        self.memory_label = QLabel("Checking this computer's and Docker's memory...")
        self.memory_label.setWordWrap(True)
        style_secondary_label(self.memory_label)
        layout.addWidget(self.memory_label)

        docker_row = QHBoxLayout()
        self.docker_label = QLabel("Checking Docker...")
        self.docker_label.setWordWrap(True)
        style_secondary_label(self.docker_label)
        docker_row.addWidget(self.docker_label, 1)
        self.docker_start_button = QPushButton("Start Docker")
        self.docker_start_button.setToolTip("Runs 'docker desktop start'.")
        self.docker_start_button.clicked.connect(partial(self._on_docker_action, "start"))
        docker_row.addWidget(self.docker_start_button)
        self.docker_restart_button = QPushButton("Restart Docker")
        self.docker_restart_button.setToolTip(
            wrap_tooltip(
                "Runs 'docker desktop restart'. Clears Docker's file sharing when C-PAC fails "
                "with 'Input/output error'. Stops any running container."
            )
        )
        self.docker_restart_button.clicked.connect(partial(self._on_docker_action, "restart"))
        docker_row.addWidget(self.docker_restart_button)
        layout.addLayout(docker_row)
        # Re-check now and then, so the warning clears (or appears) without a restart.
        self.docker_timer = QTimer(self)
        self.docker_timer.setInterval(DOCKER_RECHECK_MS)
        self.docker_timer.timeout.connect(self._probe_docker)

        mode_form = QFormLayout()
        self.mode_combo = QComboBox()
        for mode, label in RUN_MODES.items():
            self.mode_combo.addItem(label, mode)
        self.mode_combo.setToolTip(
            wrap_tooltip(
                "Skip done: subjects with every expected output are skipped; incomplete ones "
                "(e.g. a crashed run) pick up where they stopped. Resume all: runs every "
                "ticked subject, reusing finished steps. Rerun from scratch: deletes the "
                "subject's working and output folders first, e.g. after a pipeline change. "
                "Keep CPUs the same when resuming, or C-PAC may redo the BOLD steps."
            )
        )
        mode_form.addRow("Run mode:", self.mode_combo)
        layout.addLayout(mode_form)

        run_row = QHBoxLayout()
        self.process_button = QPushButton("Preprocess")
        self.process_button.setToolTip(
            wrap_tooltip(
                "Runs C-PAC's standard 'preproc' pipeline in Docker, one container per "
                "subject -- the same step as the fmri_preprocess CLI. Docker Desktop must "
                "be running; the first run downloads the ~15 GB C-PAC image."
            )
        )
        self.process_button.setStyleSheet(
            primary_action_stylesheet(PROCESS_ACCENT_COLOR, PROCESS_ACCENT_HOVER_COLOR)
        )
        self.process_button.setEnabled(False)
        self.process_button.clicked.connect(self._on_process)
        run_row.addWidget(self.process_button, 1)
        layout.addLayout(run_row)
        self.bids_group = group
        return group

    def _build_run_group(self) -> QGroupBox:
        """Large progress panel, shown only while a batch runs."""
        group = QGroupBox("Running")
        group.setStyleSheet(accent_group_box_stylesheet(PROCESS_ACCENT_COLOR))
        layout = QVBoxLayout(group)
        head = QHBoxLayout()
        self.run_title = QLabel()
        style_name_label(self.run_title)
        self.run_title.setStyleSheet(f"color: {PROCESS_ACCENT_COLOR};")
        head.addWidget(self.run_title, 1)
        self.stop_button = QPushButton("Stop after running subject(s)")
        self.stop_button.setToolTip(
            "Lets the running subject(s) finish and starts no new ones; C-PAC can't be cut "
            "off cleanly."
        )
        self.stop_button.clicked.connect(self._on_stop)
        self.stop_now_button = QPushButton("Stop now (free CPU and memory)")
        self.stop_now_button.setToolTip(
            wrap_tooltip(
                "Stops the running C-PAC container(s) at once (docker stop) and starts no new "
                "subjects, so the PC is free for other work. Nothing finished is lost: run "
                "the batch again later (Run mode 'Skip done, resume the rest') and each "
                "stopped subject picks up from its last finished step."
            )
        )
        self.stop_now_button.clicked.connect(self._on_stop_now)
        head.addWidget(self.stop_button)
        head.addWidget(self.stop_now_button)
        layout.addLayout(head)

        self.step_bar = QProgressBar()
        self.step_bar.setMinimumHeight(30)
        bar_font = self.step_bar.font()
        bar_font.setPointSize(bar_font.pointSize() + 3)
        bar_font.setBold(True)
        self.step_bar.setFont(bar_font)
        self.step_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {PROCESS_ACCENT_COLOR}; }}"
            "QProgressBar { text-align: center; }"
        )
        layout.addWidget(self.step_bar)
        self.last_step_label = QLabel()
        style_secondary_label(self.last_step_label)
        layout.addWidget(self.last_step_label)
        self.subject_bar = QProgressBar()
        layout.addWidget(self.subject_bar)

        self.spinner_index = 0
        self.spinner_timer = QTimer(self)
        self.spinner_timer.setInterval(150)
        self.spinner_timer.timeout.connect(self._update_run_title)
        self.step_estimate: int | None = None
        self.run_stage = ""
        group.setVisible(False)
        self.run_group = group
        return group

    def _build_output_group(self) -> QGroupBox:
        group = QGroupBox("Output Folder")
        group.setStyleSheet(accent_group_box_stylesheet(HOME_BASE_ACCENT_COLOR))
        layout = QVBoxLayout(group)
        self.output_path_label = QLabel("")
        style_secondary_label(self.output_path_label)
        layout.addWidget(self.output_path_label)
        self.output_name_label = QLabel("No output folder selected")
        style_name_label(self.output_name_label, HOME_BASE_NAME_EXTRA_POINT_INCREASE)
        layout.addWidget(self.output_name_label)

        buttons = QHBoxLayout()
        browse = QPushButton("Browse...")
        browse.setToolTip("Pick where C-PAC writes output/ and log/.")
        browse.clicked.connect(self._on_browse_output)
        buttons.addWidget(browse)
        self.output_reveal_button = QPushButton("Reveal")
        self.output_reveal_button.setEnabled(False)
        self.output_reveal_button.clicked.connect(partial(self._reveal, "output"))
        buttons.addWidget(self.output_reveal_button)
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Re-scan both folders for new subjects and outputs.")
        refresh.clicked.connect(self._refresh_roster)
        buttons.addWidget(refresh)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        self.output_group = group
        return group

    def _build_summary_group(self) -> QGroupBox:
        group = QGroupBox("Summary")
        group.setMinimumWidth(SUMMARY_MIN_WIDTH)  # else the grid squeezes it to a word per line
        group.setStyleSheet(accent_group_box_stylesheet(HOME_BASE_ACCENT_COLOR))
        layout = QVBoxLayout(group)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)
        return group

    # ---- folders & settings ------------------------------------------------------------------

    def _restore_settings(self) -> None:
        self.parallel_spin.setValue(int(str(self.settings.value("parallel", 1))))
        self.cpus_spin.setValue(int(str(self.settings.value("n_cpus", 1))))
        self.mem_spin.setValue(max(MIN_MEM_GB, float(str(self.settings.value("mem_gb", 0)))))
        # A deleting mode is never restored as the default for the next session.
        saved_mode = str(self.settings.value("run_mode", "skip"))
        if saved_mode != "scratch" and (index := self.mode_combo.findData(saved_mode)) >= 0:
            self.mode_combo.setCurrentIndex(index)
        self.memory_probe = MemoryProbeThread()
        self.memory_probe.done.connect(self._on_memory_probed)
        self.memory_probe.start()
        self.docker_timer.start()
        unticked_tasks = str(self.settings.value("unticked_tasks", "") or "")
        self.unticked_tasks = set(filter(None, unticked_tasks.split(",")))
        bids = str(self.settings.value("bids", "") or "")
        output = str(self.settings.value("output", "") or "")
        if output and Path(output).is_dir():
            self._set_output_folder(Path(output))
        if bids and Path(bids).is_dir():
            self._set_bids_folder(Path(bids))
        self._refresh_roster()

    def _on_browse_bids(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select BIDS folder", default_browse_dir(self.bids_folder)
        )
        if not chosen:
            return
        folder = Path(chosen)
        if self.output_folder is not None and paths_conflict(folder, self.output_folder):
            QMessageBox.warning(
                self,
                "Same as output folder",
                "The BIDS folder can't be (inside) the output folder; pick another one.",
            )
            return
        if not (folder / "dataset_description.json").is_file():
            QMessageBox.warning(
                self,
                "Doesn't look like a BIDS folder",
                "No dataset_description.json here. Point this at the converted BIDS root "
                "(run fmri_crosscheck first if you haven't).",
            )
        self._set_bids_folder(folder)
        self._refresh_roster()

    def _on_browse_output(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select output folder", default_browse_dir(self.output_folder)
        )
        if not chosen:
            return
        folder = Path(chosen)
        if self.bids_folder is not None and paths_conflict(folder, self.bids_folder):
            QMessageBox.warning(
                self,
                "Same as BIDS folder",
                "C-PAC output can't go in (or around) the BIDS folder; pick another one.",
            )
            return
        self._set_output_folder(folder)
        self._refresh_roster()

    def _set_bids_folder(self, folder: Path) -> None:
        self.bids_folder = folder
        set_path_display(self.bids_path_label, self.bids_name_label, folder)
        self.bids_reveal_button.setEnabled(True)
        self.settings.setValue("bids", str(folder))
        self.task_list.blockSignals(True)
        self.task_list.clear()
        for label in bids_tasks(folder):
            item = QListWidgetItem(label)
            ticked = label not in self.unticked_tasks
            item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
            self.task_list.addItem(item)
        self.task_list.blockSignals(False)

    def _set_output_folder(self, folder: Path) -> None:
        self.output_folder = folder
        set_path_display(self.output_path_label, self.output_name_label, folder)
        self.output_reveal_button.setEnabled(True)
        self.settings.setValue("output", str(folder))

    def _reveal(self, which: str) -> None:
        folder = self.bids_folder if which == "bids" else self.output_folder
        if folder is not None and folder.is_dir():
            reveal(folder)

    def _all_task_labels(self) -> list[str]:
        items = (self.task_list.item(row) for row in range(self.task_list.count()))
        return [item.text() for item in items if item is not None]

    def _ticked_tasks(self) -> list[str]:
        return [label for label in self._all_task_labels() if label not in self.unticked_tasks]

    def _tasks(self) -> list[str]:
        """The task filter for C-PAC and the roster: the ticked tasks, or [] (every task)
        when all are ticked."""
        ticked = self._ticked_tasks()
        return [] if ticked == self._all_task_labels() else ticked

    def _set_ticked_tasks(self, labels: list[str]) -> None:
        self.task_list.blockSignals(True)
        for row in range(self.task_list.count()):
            item = self.task_list.item(row)
            if item is not None:
                state = Qt.CheckState.Checked if item.text() in labels else Qt.CheckState.Unchecked
                item.setCheckState(state)
        self.task_list.blockSignals(False)
        self.unticked_tasks = set(self._all_task_labels()) - set(labels)
        self._on_tasks_changed()

    def _tick_all_tasks(self, ticked: bool) -> None:
        self._set_ticked_tasks(self._all_task_labels() if ticked else [])

    def _on_task_ticked(self, item: QListWidgetItem) -> None:
        if item.checkState() == Qt.CheckState.Checked:
            self.unticked_tasks.discard(item.text())
        else:
            self.unticked_tasks.add(item.text())
        self._on_tasks_changed()

    def _on_tasks_changed(self) -> None:
        # Untick-list rather than tick-list, so new tasks start ticked.
        self.settings.setValue("unticked_tasks", ",".join(sorted(self.unticked_tasks)))
        self._refresh_roster()

    # ---- roster ------------------------------------------------------------------------------

    def _subject_status(self, subject: str) -> str:
        if subject in self.running:
            return "running"
        if self.bids_folder is None or self.output_folder is None:
            return "not run"
        tasks = self._tasks()
        if is_subject_done(self.bids_folder, self.output_folder, subject, tasks):
            return "done"
        if self._tasks_to_do(subject) != subject_tasks_in(self.bids_folder, subject, tasks):
            return "partial"  # several tasks ticked, some already preprocessed on their own
        console_log = subject_console_log(self.output_folder, subject)
        if console_log.is_file():
            return "stopped" if was_stopped(console_log) else "failed"
        return "not run"

    def _tasks_to_do(self, subject: str) -> list[str]:
        """This subject's ticked tasks that aren't fully preprocessed yet."""
        if self.bids_folder is None or self.output_folder is None:
            return []
        return [
            label
            for label in subject_tasks_in(self.bids_folder, subject, self._tasks())
            if not is_subject_done(self.bids_folder, self.output_folder, subject, label)
        ]

    def _refresh_roster(self) -> None:
        selected = set(self._selected_subjects())
        task = self._tasks()
        ticked_any = bool(self._ticked_tasks())
        self.subjects = (
            bids_subjects(self.bids_folder, task) if self.bids_folder and ticked_any else []
        )
        self.statuses = {subject: self._subject_status(subject) for subject in self.subjects}
        self.subject_table.blockSignals(True)
        self.subject_table.setRowCount(len(self.subjects))
        for row, subject in enumerate(self.subjects):
            sessions = bold_sessions(self.bids_folder, subject, task) if self.bids_folder else []
            name_item = QTableWidgetItem(f"sub-{subject}")
            ticked = subject not in self.unticked
            name_item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
            self.subject_table.setItem(row, 0, name_item)
            self.subject_table.setItem(row, 1, QTableWidgetItem(str(len(sessions))))
            self.subject_table.setItem(row, 2, self._status_item(self.statuses[subject]))
            if subject in selected:
                self.subject_table.selectRow(row)
        self.subject_table.blockSignals(False)
        self._refresh_summary()
        self._update_buttons()
        self._render_detail()

    def _status_item(self, status: str) -> QTableWidgetItem:
        icon, color = STATUS_ICON[status]
        item = QTableWidgetItem(icon)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setForeground(QColor(color))
        item.setToolTip(f"{status}\n\n{STATUS_LEGEND}")
        return item

    def _set_status(self, subject: str, status: str) -> None:
        self.statuses[subject] = status
        if subject in self.subjects:
            self.subject_table.setItem(self.subjects.index(subject), 2, self._status_item(status))
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        if self.bids_folder is None:
            self.summary_label.setText("Choose a BIDS folder and an output folder.")
            return
        if not self._ticked_tasks():
            self.summary_label.setText("No tasks ticked: tick at least one task.")
            return
        what = f"{task_names(self._tasks())} BOLD" if self._tasks() else "BOLD runs"
        counts = {status: list(self.statuses.values()).count(status) for status in STATUS_ICON}
        lines = [f"{len(self.subjects)} subject(s) with {what}"]
        lines += [
            f"{STATUS_ICON[status][0]} {count} {status}"
            for status, count in counts.items()
            if count
        ]
        if self.output_folder is None:
            lines.append("Choose an output folder.")
        self.summary_label.setText("\n".join(lines))

    def _selected_subjects(self) -> list[str]:
        model = self.subject_table.selectionModel()
        rows = sorted(index.row() for index in model.selectedRows()) if model else []
        return [self.subjects[row] for row in rows if row < len(self.subjects)]

    def _on_memory_probed(
        self, docker_gb: float | None, docker_cpus: int | None, docker_error: str
    ) -> None:
        self.memory_probe.wait()
        self.docker_gb = docker_gb
        self.docker_cpus = docker_cpus
        self.docker_error = docker_error
        self.memory_probed = True
        self._update_memory_label()
        self._update_docker_label()

    def _probe_docker(self) -> None:
        """Re-ask Docker whether it runs (and its memory), unless a check or start is busy."""
        if self.memory_probe.isRunning() or self.docker_control is not None:
            return
        self.memory_probe.start()

    def _update_docker_label(self) -> None:
        if self.docker_control is not None:
            text = f"Docker: '{self.docker_control.action}' in progress (up to a few minutes)..."
            color = RUNNING_COLOR
        elif self.docker_error:
            text, color = f"⚠ Docker isn't running: {self.docker_error}", ERROR_COLOR
        else:
            text, color = "✓ Docker is running.", OK_COLOR
        self.docker_label.setText(text)
        self.docker_label.setStyleSheet(f"color: {color};")
        busy = self.docker_control is not None
        self.docker_start_button.setEnabled(not busy and bool(self.docker_error))
        self.docker_restart_button.setEnabled(not busy)

    def _on_docker_action(self, action: str) -> None:
        if self.docker_control is not None:
            return
        self._log_activity(f"Docker: running 'docker desktop {action}'...")
        self.docker_control = DockerControlThread(action)
        self.docker_control.done.connect(self._on_docker_action_done)
        self._update_docker_label()
        self.docker_control.start()

    def _on_docker_action_done(self, action: str, error: str) -> None:
        if self.docker_control is not None:
            self.docker_control.wait()
        self.docker_control = None
        if error:
            self._log_activity(f"Docker {action} failed: {error}", True)
        else:
            self._log_activity(f"Docker {action}: done.")
        self._update_docker_label()
        self._probe_docker()

    def _plan(self) -> ParallelPlan:
        return ParallelPlan(
            self.parallel_spin.value(), self.cpus_spin.value(), self.mem_spin.value()
        )

    def _suggested_plan(self) -> ParallelPlan:
        cpus = self.docker_cpus or os.cpu_count()
        return suggest_plan(cpus, self.docker_gb, len(self._ticked_subjects()))

    def _on_suggest(self) -> None:
        plan = self._suggested_plan()
        self.parallel_spin.setValue(plan.parallel)
        self.cpus_spin.setValue(plan.n_cpus)
        self.mem_spin.setValue(plan.mem_gb)

    def _update_memory_label(self) -> None:
        if not self.memory_probed:
            return
        plan = self._plan()
        problem = memory_problem(plan.mem_gb, self.docker_gb, self.host_gb)
        warning = parallel_warning(plan, self.docker_gb, self.docker_cpus)
        host = f"{self.host_gb:.1f} GB" if self.host_gb is not None else "unknown"
        cpus = f", {self.docker_cpus} CPUs" if self.docker_cpus else ""
        if problem is not None:
            text, color = f"⚠ {problem}", ERROR_COLOR
        elif self.docker_gb is None:
            text, color = f"This computer: {host} RAM. Docker's limit couldn't be read.", None
        elif warning is not None:
            text, color = f"⚠ {warning}", WARNING_COLOR
        else:
            text = f"✓ Docker may use {self.docker_gb:.1f} GB of this computer's {host} RAM{cpus}."
            color = OK_COLOR
        if plan.parallel > 1:
            text += (
                f"\nIn use at once: {plan.parallel * plan.n_cpus} CPUs, "
                f"{plan.parallel * plan.mem_gb:g} GB."
            )
        suggested = self._suggested_plan()
        if suggested != plan and self.docker_gb is not None:
            text += (
                f"\nSuggested here: {suggested.parallel} at once x {suggested.n_cpus} CPUs, "
                f"{suggested.mem_gb:g} GB (click 'Suggest')."
            )
        self.memory_label.setText(text)
        style = f"color: {color};" if color else f"color: {UNKNOWN_COLOR};"
        self.memory_label.setStyleSheet(style)

    def _ticked_subjects(self) -> list[str]:
        return [subject for subject in self.subjects if subject not in self.unticked]

    def _on_tick_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or item.row() >= len(self.subjects):
            return
        subject = self.subjects[item.row()]
        if item.checkState() == Qt.CheckState.Checked:
            self.unticked.discard(subject)
        else:
            self.unticked.add(subject)
        self._update_buttons()

    def _tick_all(self, ticked: bool) -> None:
        state = Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked
        for row in range(self.subject_table.rowCount()):
            item = self.subject_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)  # itemChanged keeps self.unticked in step

    def _update_buttons(self) -> None:
        # While running, the folder/settings panels and the tickboxes are locked; the
        # subject list stays clickable (it only shows logs and outputs).
        idle = self.worker is None
        self.bids_group.setEnabled(idle)
        self.output_group.setEnabled(idle)
        self.tick_all_button.setEnabled(idle)
        self.tick_none_button.setEnabled(idle)
        tickable = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if idle:
            tickable |= Qt.ItemFlag.ItemIsUserCheckable
        self.subject_table.blockSignals(True)
        for row in range(self.subject_table.rowCount()):
            item = self.subject_table.item(row, 0)
            if item is not None:
                item.setFlags(tickable)
        self.subject_table.blockSignals(False)
        n_ticked = len(self._ticked_subjects())
        self.process_button.setText(f"Preprocess {n_ticked} ticked subject(s)")
        self.process_button.setEnabled(idle and n_ticked > 0 and self.output_folder is not None)
        self._update_memory_label()  # the suggestion depends on how many are ticked

    # ---- detail pane -------------------------------------------------------------------------

    def _render_detail(self) -> None:
        while self.outputs_layout.count():
            item = self.outputs_layout.takeAt(0)
            if item is not None and (widget := item.widget()) is not None:
                widget.deleteLater()
        selected = self._selected_subjects()
        if len(selected) != 1 or self.output_folder is None:
            hint = "Select one subject to see its outputs and C-PAC log."
            self.outputs_layout.addWidget(QLabel(hint))
            self.outputs_layout.addStretch(1)
            self.log_text.setPlainText("")
            self._show_previews(None)
            return
        subject = selected[0]
        self._show_previews(subject)
        title = QLabel(f"<h2>sub-{subject}</h2>")
        self.outputs_layout.addWidget(title)
        if self.statuses.get(subject) in ("failed", "stopped"):
            self.outputs_layout.addWidget(self._failure_group(subject, self.output_folder))
        if self.statuses.get(subject) == "partial":
            self.outputs_layout.addWidget(self._partial_group(self._tasks_to_do(subject)))
        if self.bids_folder is not None and self.statuses.get(subject) != "running":
            task = self._tasks()
            sessions = bold_sessions(self.bids_folder, subject, task)
            gaps = missing_outputs(self.output_folder, subject, sessions, task)
            if any(gaps.values()):
                self.outputs_layout.addWidget(self._missing_group(gaps))
        bolds = preprocessed_bolds(self.output_folder, subject)
        group = QGroupBox(f"Preprocessed BOLD ({len(bolds)})")
        grid = QGridLayout(group)
        if not bolds:
            grid.addWidget(QLabel("None yet."), 0, 0)
        for row, bold in enumerate(bolds):
            grid.addWidget(QLabel(bold.name), row, 0)
            reveal_button = QPushButton("Reveal")
            reveal_button.clicked.connect(partial(reveal, bold.parent))
            grid.addWidget(reveal_button, row, 1)
        grid.setColumnStretch(0, 1)
        self.outputs_layout.addWidget(group)
        self.outputs_layout.addStretch(1)
        self._show_log(subject)

    def _partial_group(self, tasks_to_do: list[str]) -> QGroupBox:
        """Which tasks an 'All tasks' run would still preprocess for this subject."""
        group = QGroupBox("Not all tasks preprocessed")
        group.setStyleSheet(accent_group_box_stylesheet(WARNING_COLOR))
        layout = QVBoxLayout(group)
        names = ", ".join(f"task-{label}" for label in tasks_to_do)
        label = QLabel(
            f"Still to do: {names}. Run with these tasks ticked and Run mode 'Skip done, "
            "resume the rest': finished steps are reused."
        )
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {WARNING_COLOR};")
        layout.addWidget(label)
        return group

    def _failure_group(self, subject: str, output_folder: Path) -> QGroupBox:
        """Why this subject's last C-PAC run failed (or that it was stopped), read back
        from its saved log."""
        stopped = self.statuses.get(subject) == "stopped"
        color = WARNING_COLOR if stopped else ERROR_COLOR
        group = QGroupBox("Stopped before it finished" if stopped else "Why it failed")
        group.setStyleSheet(accent_group_box_stylesheet(color))
        layout = QVBoxLayout(group)
        cause = explain_failure(subject_console_log(output_folder, subject), output_folder)
        label = QLabel(cause or "No known cause recognised; see the C-PAC Log tab.")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet(f"color: {color}; font-weight: bold;")
        layout.addWidget(label)
        return group

    def _missing_group(self, gaps: dict[str, list[str]]) -> QGroupBox:
        """The outputs C-PAC said it would write (expectedOutputs.yml) but didn't, per session."""
        n_missing = sum(map(len, gaps.values()))
        group = QGroupBox(f"Not complete: {n_missing} expected output(s) missing")
        group.setStyleSheet(accent_group_box_stylesheet(WARNING_COLOR))
        layout = QVBoxLayout(group)
        hint = QLabel(
            "Run it again with Run mode 'Skip done, resume the rest' (same CPUs): finished "
            "steps are reused, so only what's missing is redone."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        lines = [f"ses-{session}  {name}" for session, names in gaps.items() for name in names]
        if len(lines) > MAX_MISSING_SHOWN:
            hidden = len(lines) - MAX_MISSING_SHOWN
            lines = [*lines[:MAX_MISSING_SHOWN], f"... and {hidden} more"]
        listing = QLabel("\n".join(lines))
        listing.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        listing.setStyleSheet(f"color: {WARNING_COLOR};")
        layout.addWidget(listing)
        return group

    def _show_previews(self, subject: str | None) -> None:
        """Refill the QC Figures, Brain Slices and Motion tabs for subject (None clears them)."""
        figures: list[QcFigure] = []
        slices: list[SlicePreview] = []
        tables: list[Path] = []
        if subject is not None and self.output_folder is not None:
            figures = qc_figures(self.output_folder, subject, self._tasks())
            slices = slice_previews(self.output_folder, subject, self._tasks())
            tables = motion_summaries(self.output_folder, subject, self._tasks())
        self.figure_picker.set_items(figures)
        self.slice_picker.set_items(slices)
        self._fill_motion(tables)
        for picker, name, count in (
            (self.figure_picker, "QC Figures", len(figures)),
            (self.slice_picker, "Brain Slices", len(slices)),
        ):
            self.detail_tabs.setTabText(self.detail_tabs.indexOf(picker), f"{name} ({count})")

    def _load_slices(self, preview: SlicePreview) -> QPixmap | str:
        """Render (or reuse) one slice preview; re-rendered when C-PAC rewrites the file."""
        try:
            key = (preview, preview.path.stat().st_mtime)
            if key not in self.slice_cache:
                if len(self.slice_cache) >= SLICE_CACHE_SIZE:
                    self.slice_cache.clear()
                self.slice_cache[key] = figure_to_pixmap(render_slices(preview))
        except (OSError, EOFError, ValueError, zlib.error, ImageFileError) as e:
            log.warning("Couldn't render %s: %s", preview.path, e)
            return f"Couldn't read {preview.path.name}:\n{e}"
        return self.slice_cache[key]

    def _fill_motion(self, tables: list[Path]) -> None:
        """One row per run (session/task), with the TSV's own columns."""
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
        self.motion_table.clear()
        self.motion_table.setColumnCount(len(columns) + 1)
        self.motion_table.setHorizontalHeaderLabels(["Run", *columns])
        self.motion_table.setRowCount(len(rows))
        for row_index, (run, values) in enumerate(rows):
            self.motion_table.setItem(row_index, 0, QTableWidgetItem(run))
            for column_index, name in enumerate(columns, start=1):
                item = QTableWidgetItem(values.get(name, ""))
                self.motion_table.setItem(row_index, column_index, item)
        self.motion_table.resizeColumnsToContents()
        motion_tab = self.detail_tabs.indexOf(self.motion_tab)
        self.detail_tabs.setTabText(motion_tab, f"Motion ({len(rows)})")

    def _show_log(self, subject: str) -> None:
        if self.output_folder is None:
            return
        console_log = subject_console_log(self.output_folder, subject)
        try:
            text = read_log_tail(console_log)
        except FileNotFoundError:
            text = "No C-PAC run logged for this subject yet."
        except OSError as e:
            text = f"Couldn't read {console_log}: {e}"
        self.log_text.setPlainText(text)
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # ---- running -----------------------------------------------------------------------------

    def _on_process(self) -> None:
        if self.bids_folder is None or self.output_folder is None:
            return
        subjects = self._ticked_subjects()
        if not subjects:
            QMessageBox.information(self, "No subjects", "Tick at least one subject first.")
            return
        if self.docker_error:
            QMessageBox.warning(
                self,
                "Docker isn't running",
                f"{self.docker_error}\n\nClick 'Start Docker' (or start Docker Desktop), wait "
                "for the green 'Docker is running', then try again.",
            )
            self._probe_docker()
            return
        mode = str(self.mode_combo.currentData())
        if mode == "scratch":
            answer = QMessageBox.question(
                self,
                "Rerun from scratch?",
                f"This deletes the C-PAC working and output folders of {len(subjects)} "
                "subject(s) before running them again (logs are kept). Continue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        plan = self._plan()
        warning = parallel_warning(plan, self.docker_gb, self.docker_cpus)
        if warning is not None and plan.parallel > 1:
            suggested = self._suggested_plan()
            answer = QMessageBox.question(
                self,
                "More than Docker allows?",
                f"{warning}\n\nSuggested for this computer: {suggested.parallel} at once x "
                f"{suggested.n_cpus} CPUs, {suggested.mem_gb:g} GB ('Suggest' fills it in). "
                "Run anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.settings.setValue("parallel", plan.parallel)
        self.settings.setValue("n_cpus", plan.n_cpus)
        self.settings.setValue("mem_gb", plan.mem_gb)
        self.settings.setValue("run_mode", mode)
        run = RunSettings(
            bids_root=self.bids_folder,
            output_root=self.output_folder,
            subjects=subjects,
            tasks=self._tasks(),
            n_cpus=plan.n_cpus,
            mem_gb=plan.mem_gb,
            mode=mode,
            parallel=min(plan.parallel, len(subjects)),
        )
        self.output_folder.mkdir(parents=True, exist_ok=True)
        self.worker = PreprocessThread(run)
        self.worker.message.connect(self._log_activity)
        self.worker.subject_started.connect(self._on_subject_started)
        self.worker.step.connect(self._on_step)
        self.worker.subject_finished.connect(self._on_subject_finished)
        self.worker.batch_done.connect(self._on_batch_done)
        what = task_names(run.tasks)
        at_once = f", {run.parallel} at once" if run.parallel > 1 else ""
        self._log_activity(
            f"Preprocessing {len(subjects)} subject(s), {what}{at_once}, "
            f"{run.n_cpus} CPU(s), {run.mem_gb:g} GB each..."
        )
        self.run_stage = "Checking Docker..."
        self.running.clear()
        self.step_estimate = int(str(self.settings.value(self._estimate_key(), 0))) or None
        self.step_bar.setRange(0, 0)
        self.last_step_label.setText("")
        self.subject_bar.setRange(0, len(subjects))
        self.subject_bar.setValue(0)
        self.subject_bar.setFormat("Subjects: %v of %m done")
        self.stop_button.setEnabled(True)
        self.stop_now_button.setEnabled(True)
        self.run_group.setVisible(True)
        self.spinner_timer.start()
        self._update_run_title()
        self._update_buttons()
        self.worker.start()

    def _estimate_key(self) -> str:
        return f"steps_per_subject/{'+'.join(self._tasks()) or 'all'}"

    def _step_count(self, steps: int) -> str:
        return f"step {steps} of ~{self.step_estimate}" if self.step_estimate else f"step {steps}"

    def _update_run_title(self) -> None:
        self.spinner_index = (self.spinner_index + 1) % len(SPINNER)
        spinner = SPINNER[self.spinner_index]
        if not self.running:
            self.run_title.setText(f"{spinner}  {self.run_stage}")
        elif len(self.running) == 1:
            subject, state = next(iter(self.running.items()))
            self.run_title.setText(f"{spinner}  sub-{subject}  -  {self._step_count(state.steps)}")
        else:
            names = ", ".join(f"sub-{subject}" for subject in self.running)
            self.run_title.setText(f"{spinner}  {len(self.running)} at once: {names}")

    def _update_step_bar(self) -> None:
        """One subject: its steps; several: their average (against the last run's count)."""
        if not self.running:
            return
        if not self.step_estimate:
            self.step_bar.setRange(0, 0)  # first run: busy bar; the title shows steps
        else:
            mean = sum(state.steps for state in self.running.values()) // len(self.running)
            self.step_bar.setRange(0, max(self.step_estimate, mean + 1))
            self.step_bar.setValue(mean)
            average = " on average" if len(self.running) > 1 else ""
            self.step_bar.setFormat(f"%p%{average}  (estimated from the last complete run)")
        self.last_step_label.setText(
            "\n".join(
                f"sub-{subject}: {self._step_count(state.steps)}"
                f"{f' - last finished: {state.last_step}' if state.last_step else ''}"
                for subject, state in self.running.items()
            )
        )

    def _on_stop(self) -> None:
        if self.worker is not None:
            self.worker.stop_requested = True
            self.stop_button.setEnabled(False)
            self._log_activity("Stopping once the running subject(s) finish...")

    def _on_stop_now(self) -> None:
        worker = self.worker
        if worker is None:
            return
        answer = QMessageBox.question(
            self,
            "Stop now?",
            f"Stop the {len(self.running) or 'running'} C-PAC container(s) now and start no "
            "new subjects? This frees the CPUs and memory. Finished steps are kept: running "
            "the batch again later resumes each subject where it stopped.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.stop_button.setEnabled(False)
        self.stop_now_button.setEnabled(False)
        self.run_stage = "Stopping..."
        self._log_activity("Stopping the running C-PAC container(s) now...")
        threading.Thread(target=worker.stop_now, daemon=True).start()  # docker takes ~10 s

    def _on_subject_started(self, _index: int, _total: int, subject: str) -> None:
        if not self.running:  # re-read: the last subject may have just saved a new count
            self.step_estimate = int(str(self.settings.value(self._estimate_key(), 0))) or None
        self.running[subject] = RunningSubject(time.monotonic())
        self._set_status(subject, "running")
        self._update_step_bar()
        self._update_run_title()
        self._update_live_line()

    def _on_step(self, subject: str, node: str, steps_done: int) -> None:
        state = self.running.get(subject)
        if state is None:
            return
        state.steps, state.last_step = steps_done, node
        self._update_step_bar()
        self._update_run_title()
        self._update_live_line()

    def _on_subject_finished(self, subject: str, steps_done: int, preprocessed: bool) -> None:
        if preprocessed and steps_done:
            self.settings.setValue(self._estimate_key(), steps_done)
        self.running.pop(subject, None)
        self._update_live_line()  # the thread's result line replaces this subject's
        if not self.running:
            self.run_stage = "Next subject..."
        self._set_status(subject, self._subject_status(subject))
        self.subject_bar.setValue(self.subject_bar.value() + 1)
        self._update_step_bar()
        self._update_run_title()
        if self._selected_subjects() == [subject]:
            self._render_detail()

    def _on_batch_done(self, result: RunSummary | str) -> None:
        if self.worker is not None:
            self.worker.wait()
        self.worker = None
        self.running.clear()
        self._set_live_line(None)
        self.spinner_timer.stop()
        self.run_group.setVisible(False)
        self._update_buttons()
        if isinstance(result, str):
            QMessageBox.warning(self, "Preprocessing not started", result)
            self._log_activity(result, is_error=True)
        else:
            stopped = " (stopped early)" if result.stopped else ""
            self._log_activity(
                f"Done{stopped}: {len(result.done)} preprocessed, {len(result.skipped)} "
                f"skipped (already done), {len(result.failed)} failed."
            )
            if result.failed:  # each cause was logged as it happened, above
                failed = ", ".join(f"sub-{subject}" for subject, _why in result.failed)
                self._log_activity(f"Failed: {failed}", is_error=True)
        self._refresh_roster()

    def _log_activity(self, message: str, is_error: bool = False) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        text = html.escape(f"[{stamp}] {'⚠ ' if is_error else ''}{message}")
        if is_error:
            text = f'<span style="color:{ERROR_COLOR};">{text}</span>'
        live = self.live_line
        self._set_live_line(None)  # keep the live line last: lift it, append, put it back
        self.activity_text.append(text)
        self._set_live_line(live)

    def _set_live_line(self, text: str | None) -> None:
        """'BUSY' line(s) at the bottom of the Activity Log, one per running subject,
        rewritten in place on each C-PAC step so hundreds of steps don't flood the log;
        None removes them."""
        if self.live_line is not None:
            cursor = self.activity_text.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            # Selects the last block including the separator before it, removing it whole
            # (the lines are <br>-joined, so they are one block).
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
        self.live_line = text
        if text is not None:
            lines = "<br>".join(html.escape(line) for line in text.split("\n"))
            self.activity_text.append(
                f'<span style="color:{RUNNING_COLOR}; font-weight:bold;">{lines}</span>'
            )

    def _update_live_line(self) -> None:
        lines = []
        for subject, state in self.running.items():
            elapsed = int(time.monotonic() - state.started_at)
            count = f"step {state.steps}"
            if self.step_estimate:
                percent = min(99, 100 * state.steps // self.step_estimate)
                count += f" of ~{self.step_estimate} ({percent}%)"
            spinner = SPINNER[self.spinner_index]
            text = f"{spinner} BUSY sub-{subject}: {count}, {elapsed // 60}m{elapsed % 60:02d}s"
            lines.append(f"{text} - {state.last_step or 'starting container'}")
        self._set_live_line("\n".join(lines) or None)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 -- Qt override
        if self.worker is None:
            self.docker_timer.stop()
            self.memory_probe.wait()  # seconds at most; a running QThread can't be destroyed
            if self.docker_control is not None:
                self.docker_control.wait()
            event.accept()
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("C-PAC is still running")
        box.setText(
            "Stop C-PAC and close, freeing the CPUs and memory? Finished steps are kept: "
            "running the batch again later resumes each subject where it stopped.\n\n"
            "Or close and leave the running container(s) going in Docker (the rest of the "
            "batch won't start)."
        )
        stop = box.addButton("Stop C-PAC and close", QMessageBox.ButtonRole.AcceptRole)
        leave = box.addButton("Leave running and close", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() not in (stop, leave):
            event.ignore()
            return
        self.settings.sync()
        if box.clickedButton() is stop:
            self.setEnabled(False)
            self._log_activity("Stopping C-PAC before closing (up to ~10 s)...")
            QApplication.processEvents()
            stopped = self.worker.stop_now()
            if self.output_folder is not None:  # the worker threads won't get to it
                for subject in stopped:
                    mark_stopped(subject_console_log(self.output_folder, subject))
        # A QThread destroyed while running aborts the process with a crash dialog, and the
        # worker may be blocked on docker for hours: leave at once.
        os._exit(0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = QApplication(sys.argv)
    window = PreprocessWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
