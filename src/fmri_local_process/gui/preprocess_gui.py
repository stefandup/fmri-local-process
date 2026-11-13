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
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QTextCursor
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
from fmri_local_process.processing.cpac_docker import (
    CPAC_IMAGE,
    MIN_MEM_GB,
    DockerUnavailableError,
    bids_subjects,
    bids_tasks,
    bold_sessions,
    clear_subject_results,
    docker_desktop,
    docker_memory_gb,
    explain_failure,
    find_docker,
    finished_node,
    host_memory_gb,
    image_present,
    is_preprocessed,
    memory_problem,
    missing_outputs,
    preprocess_subject,
    preprocessed_bolds,
    run_cpac,
    subject_console_log,
)

log = logging.getLogger(__name__)

SETTINGS_APP = "FmriPreprocess"
ALL_TASKS = "All tasks"
LOG_TAIL_LINES = 400
MAX_MISSING_SHOWN = 12
DOCKER_RECHECK_MS = 30_000
STATUS_ICON = {
    "done": ("✓", OK_COLOR),
    "failed": ("✗", ERROR_COLOR),
    "running": ("▶", RUNNING_COLOR),
    "not run": ("○", UNKNOWN_COLOR),
}
SPINNER = "◐◓◑◒"
STATUS_LEGEND = (
    "✓ every session has a preprocessed BOLD (of the chosen task) and every output C-PAC "
    "expected\n"
    "✗ C-PAC ran (a log exists) but its output is incomplete\n"
    "▶ running now\n"
    "○ not run yet"
)
# Run mode -> dropdown label. Every mode reuses C-PAC's working folder except "scratch".
RUN_MODES = {
    "skip": "Skip done, resume the rest",
    "resume": "Resume all (reuse finished steps)",
    "scratch": "Rerun from scratch (delete previous results)",
}


@dataclass
class RunSettings:
    bids_root: Path
    output_root: Path
    subjects: list[str]
    task: str | None
    n_cpus: int
    mem_gb: float
    mode: str = "skip"  # a RUN_MODES key
    image: str = CPAC_IMAGE


@dataclass
class RunSummary:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False


class PreprocessThread(QThread):
    """Runs the batch: Docker check, one-time image pull, then one C-PAC container per subject."""

    message = Signal(str, bool)  # text, is_error
    subject_started = Signal(int, int, str)  # index, total, subject
    step = Signal(str, str, int)  # subject, finished node, steps done
    subject_finished = Signal(str, int, bool)  # subject, steps done, preprocessed by this run
    batch_done = Signal(object)  # RunSummary, or an error message

    def __init__(self, run: RunSettings) -> None:
        super().__init__()
        self.run_settings = run
        self.stop_requested = False

    def run(self) -> None:
        run = self.run_settings
        try:
            docker = find_docker()
        except DockerUnavailableError as e:
            self.batch_done.emit(str(e))
            return
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
        for index, subject in enumerate(run.subjects):
            if self.stop_requested:
                summary.stopped = True
                break
            self.subject_started.emit(index, len(run.subjects), subject)
            sessions = bold_sessions(run.bids_root, subject, run.task)
            if run.mode == "skip" and is_preprocessed(run.output_root, subject, sessions, run.task):
                summary.skipped.append(subject)
                self.subject_finished.emit(subject, 0, False)
                continue
            if run.mode == "scratch":
                removed = clear_subject_results(run.output_root, subject)
                self.message.emit(
                    f"sub-{subject}: deleted {len(removed)} previous working/output folder(s)",
                    False,
                )
            steps_done = 0

            def on_line(line: str, subject: str = subject) -> None:
                nonlocal steps_done
                node = finished_node(line)
                if node is not None:
                    steps_done += 1
                    self.step.emit(subject, node, steps_done)

            failure = preprocess_subject(
                docker,
                run.bids_root,
                run.output_root,
                subject,
                run.image,
                run.n_cpus,
                run.mem_gb,
                run.task,
                on_line,
            )
            if failure is None:
                summary.done.append(subject)
                self.message.emit(f"sub-{subject}: preprocessed ({steps_done} steps)", False)
            else:
                summary.failed.append((subject, failure))
                self.message.emit(f"sub-{subject} FAILED: {failure}", True)
            self.subject_finished.emit(subject, steps_done, failure is None)
        self.batch_done.emit(summary)

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
    """Asks Docker for its memory limit off the GUI thread (slow when Docker is starting)."""

    done = Signal(object, str)  # Docker's limit in GB or None, why it's unknown

    def run(self) -> None:
        try:
            docker = find_docker()
        except DockerUnavailableError as e:
            self.done.emit(None, str(e))
            return
        self.done.emit(docker_memory_gb(docker), "")


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
        self.running_subject: str | None = None
        self.worker: PreprocessThread | None = None
        self.live_line: str | None = None
        self.host_gb = host_memory_gb()
        self.docker_gb: float | None = None
        self.docker_error = ""
        self.memory_probed = False
        self.docker_control: DockerControlThread | None = None
        # Untick-list rather than tick-list, so newly found subjects start ticked.
        self.unticked: set[str] = set()
        self.subject_started_at = 0.0
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
            QLabel("Tick the subjects to preprocess; click a row to see its outputs and log.")
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
        self.task_combo = QComboBox()
        self.task_combo.setToolTip("Only preprocess this task's BOLD runs.")
        self.task_combo.currentIndexChanged.connect(self._on_task_changed)
        form.addRow("Task:", self.task_combo)
        self.cpus_spin = QSpinBox()
        self.cpus_spin.setRange(1, os.cpu_count() or 1)
        self.cpus_spin.setToolTip("CPU cores C-PAC may use per subject.")
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
        self.stop_button = QPushButton("Stop after this subject")
        self.stop_button.setToolTip(
            "Lets the running subject finish, then stops; C-PAC can't be cut off cleanly."
        )
        self.stop_button.clicked.connect(self._on_stop)
        head.addWidget(self.stop_button)
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
        self.current_steps = 0
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
        group.setStyleSheet(accent_group_box_stylesheet(HOME_BASE_ACCENT_COLOR))
        layout = QVBoxLayout(group)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)
        return group

    # ---- folders & settings ------------------------------------------------------------------

    def _restore_settings(self) -> None:
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
        saved_task = str(self.settings.value("task", "") or "")
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        self.task_combo.addItems([ALL_TASKS, *bids_tasks(folder)])
        index = self.task_combo.findText(saved_task)
        self.task_combo.setCurrentIndex(max(index, 0))
        self.task_combo.blockSignals(False)

    def _set_output_folder(self, folder: Path) -> None:
        self.output_folder = folder
        set_path_display(self.output_path_label, self.output_name_label, folder)
        self.output_reveal_button.setEnabled(True)
        self.settings.setValue("output", str(folder))

    def _reveal(self, which: str) -> None:
        folder = self.bids_folder if which == "bids" else self.output_folder
        if folder is not None and folder.is_dir():
            reveal(folder)

    def _task(self) -> str | None:
        text = self.task_combo.currentText()
        return None if text in ("", ALL_TASKS) else text

    def _on_task_changed(self) -> None:
        self.settings.setValue("task", self._task() or "")
        self._refresh_roster()

    # ---- roster ------------------------------------------------------------------------------

    def _subject_status(self, subject: str) -> str:
        if subject == self.running_subject:
            return "running"
        if self.bids_folder is None or self.output_folder is None:
            return "not run"
        task = self._task()
        sessions = bold_sessions(self.bids_folder, subject, task)
        if is_preprocessed(self.output_folder, subject, sessions, task):
            return "done"
        if subject_console_log(self.output_folder, subject).is_file():
            return "failed"
        return "not run"

    def _refresh_roster(self) -> None:
        selected = set(self._selected_subjects())
        task = self._task()
        self.subjects = bids_subjects(self.bids_folder, task) if self.bids_folder else []
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
        what = f"task-{self._task()} BOLD" if self._task() else "BOLD runs"
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

    def _on_memory_probed(self, docker_gb: float | None, docker_error: str) -> None:
        self.memory_probe.wait()
        self.docker_gb = docker_gb
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

    def _update_memory_label(self) -> None:
        if not self.memory_probed:
            return
        problem = memory_problem(self.mem_spin.value(), self.docker_gb, self.host_gb)
        host = f"{self.host_gb:.1f} GB" if self.host_gb is not None else "unknown"
        if problem is not None:
            text, color = f"⚠ {problem}", ERROR_COLOR
        elif self.docker_gb is None:
            text, color = f"This computer: {host} RAM. Docker's limit couldn't be read.", None
        else:
            text = f"✓ Docker may use {self.docker_gb:.1f} GB of this computer's {host} RAM."
            color = OK_COLOR
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
            return
        subject = selected[0]
        title = QLabel(f"<h2>sub-{subject}</h2>")
        self.outputs_layout.addWidget(title)
        if self.statuses.get(subject) == "failed":
            self.outputs_layout.addWidget(self._failure_group(subject, self.output_folder))
        if self.bids_folder is not None and self.statuses.get(subject) != "running":
            task = self._task()
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

    def _failure_group(self, subject: str, output_folder: Path) -> QGroupBox:
        """Why this subject's last C-PAC run failed, read back from its saved log."""
        group = QGroupBox("Why it failed")
        group.setStyleSheet(accent_group_box_stylesheet(ERROR_COLOR))
        layout = QVBoxLayout(group)
        cause = explain_failure(subject_console_log(output_folder, subject), output_folder)
        label = QLabel(cause or "No known cause recognised; see the C-PAC Log tab.")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet(f"color: {ERROR_COLOR}; font-weight: bold;")
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
        self.settings.setValue("n_cpus", self.cpus_spin.value())
        self.settings.setValue("mem_gb", self.mem_spin.value())
        self.settings.setValue("run_mode", mode)
        run = RunSettings(
            bids_root=self.bids_folder,
            output_root=self.output_folder,
            subjects=subjects,
            task=self._task(),
            n_cpus=self.cpus_spin.value(),
            mem_gb=self.mem_spin.value(),
            mode=mode,
        )
        self.output_folder.mkdir(parents=True, exist_ok=True)
        self.worker = PreprocessThread(run)
        self.worker.message.connect(self._log_activity)
        self.worker.subject_started.connect(self._on_subject_started)
        self.worker.step.connect(self._on_step)
        self.worker.subject_finished.connect(self._on_subject_finished)
        self.worker.batch_done.connect(self._on_batch_done)
        what = f"task-{run.task}" if run.task else "all tasks"
        self._log_activity(
            f"Preprocessing {len(subjects)} subject(s), {what}, "
            f"{run.n_cpus} CPU(s), {run.mem_gb:g} GB each..."
        )
        self.run_stage = "Checking Docker..."
        self.current_steps = 0
        self.step_estimate = None
        self.step_bar.setRange(0, 0)
        self.last_step_label.setText("")
        self.subject_bar.setRange(0, len(subjects))
        self.subject_bar.setValue(0)
        self.subject_bar.setFormat("Subjects: %v of %m done")
        self.stop_button.setEnabled(True)
        self.run_group.setVisible(True)
        self.spinner_timer.start()
        self._update_run_title()
        self._update_buttons()
        self.worker.start()

    def _estimate_key(self) -> str:
        return f"steps_per_subject/{self._task() or 'all'}"

    def _update_run_title(self) -> None:
        self.spinner_index = (self.spinner_index + 1) % len(SPINNER)
        spinner = SPINNER[self.spinner_index]
        if self.running_subject is None:
            self.run_title.setText(f"{spinner}  {self.run_stage}")
            return
        count = f"step {self.current_steps}"
        if self.step_estimate:
            count += f" of ~{self.step_estimate}"
        self.run_title.setText(f"{spinner}  sub-{self.running_subject}  -  {count}")

    def _on_stop(self) -> None:
        if self.worker is not None:
            self.worker.stop_requested = True
            self.stop_button.setEnabled(False)
            self._log_activity("Stopping after the current subject...")

    def _on_subject_started(self, index: int, total: int, subject: str) -> None:
        self.subject_bar.setValue(index)
        self.current_steps = 0
        estimate = int(str(self.settings.value(self._estimate_key(), 0)))
        self.step_estimate = estimate or None
        if self.step_estimate:
            # Known total from the last complete run: a real bar.
            self.step_bar.setRange(0, self.step_estimate)
            self.step_bar.setValue(0)
            self.step_bar.setFormat("%p%  (estimated from the last complete run)")
        else:
            self.step_bar.setRange(0, 0)  # first run: busy bar; the big counter shows steps
        self.last_step_label.setText("Starting container (or checking for existing output)...")
        self.running_subject = subject
        self.subject_started_at = time.monotonic()
        self._set_status(subject, "running")
        self._update_run_title()
        self._update_live_line("starting container")

    def _on_step(self, subject: str, node: str, steps_done: int) -> None:
        self.current_steps = steps_done
        if self.step_estimate:
            if steps_done >= self.step_bar.maximum():  # more steps than last time
                self.step_bar.setMaximum(steps_done + 1)
            self.step_bar.setValue(steps_done)
        self.last_step_label.setText(f"Last finished step: {node}")
        self._update_run_title()
        self._update_live_line(node)

    def _on_subject_finished(self, subject: str, steps_done: int, preprocessed: bool) -> None:
        if preprocessed and steps_done:
            self.settings.setValue(self._estimate_key(), steps_done)
        self._set_live_line(None)  # the thread's result line replaces it
        self.running_subject = None
        self.run_stage = "Next subject..."
        self._set_status(subject, self._subject_status(subject))
        self.subject_bar.setValue(self.subject_bar.value() + 1)
        if self._selected_subjects() == [subject]:
            self._render_detail()

    def _on_batch_done(self, result: RunSummary | str) -> None:
        if self.worker is not None:
            self.worker.wait()
        self.worker = None
        self.running_subject = None
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
        """One 'BUSY' line at the bottom of the Activity Log, rewritten in place on each
        C-PAC step so hundreds of steps don't flood the log; None removes it."""
        if self.live_line is not None:
            cursor = self.activity_text.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            # Selects the last block including the separator before it, removing it whole.
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
        self.live_line = text
        if text is not None:
            self.activity_text.append(
                f'<span style="color:{RUNNING_COLOR}; font-weight:bold;">{html.escape(text)}</span>'
            )

    def _update_live_line(self, last_step: str = "") -> None:
        if self.running_subject is None:
            return
        elapsed = int(time.monotonic() - self.subject_started_at)
        count = f"step {self.current_steps}"
        if self.step_estimate:
            percent = min(99, 100 * self.current_steps // self.step_estimate)
            count += f" of ~{self.step_estimate} ({percent}%)"
        text = (
            f"{SPINNER[self.spinner_index]} BUSY sub-{self.running_subject}: {count}, "
            f"{elapsed // 60}m{elapsed % 60:02d}s"
        )
        self._set_live_line(f"{text} - {last_step}" if last_step else text)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 -- Qt override
        if self.worker is None:
            self.docker_timer.stop()
            self.memory_probe.wait()  # seconds at most; a running QThread can't be destroyed
            if self.docker_control is not None:
                self.docker_control.wait()
            event.accept()
            return
        answer = QMessageBox.question(
            self,
            "C-PAC is still running",
            "Closing now leaves the current C-PAC container running in Docker Desktop "
            "(stop it there if needed) and the rest of the batch won't start. Close anyway?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        # A QThread destroyed while running aborts the process with a crash dialog, and the
        # worker is blocked on docker for possibly hours: save settings and leave at once.
        self.settings.sync()
        os._exit(0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = QApplication(sys.argv)
    window = PreprocessWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
