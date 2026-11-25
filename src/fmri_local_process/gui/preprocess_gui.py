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
import threading
import zlib
from datetime import datetime
from functools import partial
from pathlib import Path

from nibabel.filebasedimages import ImageFileError
from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QGuiApplication, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
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

from fmri_local_process.app.colors import (
    ERROR_COLOR,
    RUNNING_COLOR,
)
from fmri_local_process.app.docker import DOCKER_RECHECK_MS
from fmri_local_process.app.output_help import MOTION_HELP, qc_figure_help, slice_help
from fmri_local_process.app.preprocess import (
    RUN_MODES,
    SETTINGS_APP,
    STATUS_ICON,
    STATUS_LEGEND,
    DockerProbe,
    PreprocessJob,
    Problem,
    RunSettings,
    RunSummary,
    done_lines,
    estimate_key,
    failure_problem,
    log_text,
    mark_all_stopped,
    memory_text,
    missing_problem,
    motion_rows,
    notices,
    overload_question,
    partial_problem,
    probe_docker,
    read_choices,
    rename_question,
    rename_subject,
    renumbered_problem,
    roster,
    save_choices,
    save_unticked_tasks,
    saved_estimate,
    scratch_question,
    start_text,
    stop_now_question,
    subject_status,
    suggested_plan,
    summary_text,
    task_filter,
    tasks_to_do,
)
from fmri_local_process.app.run_progress import RunningSubject
from fmri_local_process.app.state import paths_conflict
from fmri_local_process.gui.docker_controls import DockerControlThread, DockerStatus
from fmri_local_process.gui.image_download import ImageDownload
from fmri_local_process.gui.image_view import (
    HelpToggle,
    PreviewPicker,
    figure_to_pixmap,
    load_figure,
)
from fmri_local_process.gui.qt_common import (
    HOME_BASE_ACCENT_COLOR,
    HOME_BASE_NAME_EXTRA_POINT_INCREASE,
    PROCESS_ACCENT_COLOR,
    PROCESS_ACCENT_HOVER_COLOR,
    SETTINGS_ORGANIZATION,
    NoticeBanner,
    accent_group_box_stylesheet,
    default_browse_dir,
    primary_action_stylesheet,
    reveal,
    set_path_display,
    style_name_label,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.gui.run_progress import RunProgress
from fmri_local_process.processing.brain_slices import SlicePreview, render_slices, slice_previews
from fmri_local_process.processing.cpac_docker import (
    CPAC_IMAGE,
    HANG_ACTIONS,
    MIN_CPUS_PER_SUBJECT,
    MIN_MEM_GB,
    SUGGESTED_MEM_GB,
    DockerUnavailableError,
    HangPolicy,
    ParallelPlan,
    bids_tasks,
    bold_sessions,
    host_memory_gb,
    preprocessed_bolds,
)
from fmri_local_process.processing.cpac_outputs import (
    QcFigure,
    motion_summaries,
    qc_figures,
)
from fmri_local_process.processing.review_mode import package_root, settings_dir
from fmri_local_process.processing.session_sync import (
    SessionChange,
    session_changes,
)

log = logging.getLogger(__name__)

TASK_LIST_HEIGHT = 96  # ~4 rows; more tasks scroll
SUMMARY_MIN_WIDTH = 200
SLICE_CACHE_SIZE = 40  # rendered slice previews kept (~3 MB each)


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
        self.job = PreprocessJob(run, listener=self)

    def run(self) -> None:
        self.job.run()

    # The PreprocessListener methods: each call becomes a signal, queued to the GUI thread.
    def on_message(self, text: str, is_error: bool, /) -> None:
        self.message.emit(text, is_error)

    def on_subject_started(self, index: int, total: int, subject: str, /) -> None:
        self.subject_started.emit(index, total, subject)

    def on_step(self, subject: str, node: str, steps_done: int, /) -> None:
        self.step.emit(subject, node, steps_done)

    def on_subject_finished(self, subject: str, steps: int, preprocessed: bool, /) -> None:
        self.subject_finished.emit(subject, steps, preprocessed)

    def on_batch_done(self, result: RunSummary | str, /) -> None:
        self.batch_done.emit(result)


class MemoryProbeThread(QThread):
    """Asks Docker for its memory and CPU limits off the GUI thread (slow when Docker is
    starting)."""

    # Docker's GB or None, its CPUs or None, why unknown, whether the C-PAC image is here
    done = Signal(object, object, str, object)

    def run(self) -> None:
        probe = probe_docker()
        self.done.emit(probe.gb, probe.cpus, probe.error, probe.has_image)


class PreprocessWindow(QMainWindow):
    """Also the toolbox's Preprocess (C-PAC) tab: it reports when C-PAC runs and which
    folders were picked here, and takes a lock while another tab changes the BIDS folder."""

    running_changed = Signal(bool)  # C-PAC started (True) / the batch ended (False)
    folder_chosen = Signal(str, object)  # "bids" / "output", Path

    def __init__(self) -> None:
        super().__init__()
        self.external_lock = ""  # why another tab keeps Preprocess from starting ("" if not)
        self.setWindowTitle("fMRI Preprocess (C-PAC)")
        self.resize(1300, 850)
        # In a review package, its own settings file: never the computer's real settings.
        root = package_root()
        self.settings = (
            QSettings(str(settings_dir(root) / f"{SETTINGS_APP}.ini"), QSettings.Format.IniFormat)
            if root is not None
            else QSettings(SETTINGS_ORGANIZATION, SETTINGS_APP)
        )
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
        self.cpac_missing = False  # Docker runs, but the C-PAC image isn't downloaded
        self.memory_probed = False
        self.suggest_pending = False  # 'Suggest' waits for a fresh look at Docker's limits
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
        self.banner = NoticeBanner()
        root_layout.addWidget(self.banner)

        # Everything above the subjects and their outputs, in one widget so "Expand" can hide
        # it and give the outputs the whole height.
        self.top_panel = QWidget()
        top_row = QHBoxLayout(self.top_panel)
        top_row.setContentsMargins(0, 0, 0, 0)
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
        root_layout.addWidget(self.top_panel)

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
        self.expand_button = QPushButton()
        self.expand_button.setCheckable(True)
        self.expand_button.toggled.connect(self._set_expanded)
        self.detail_tabs.setCornerWidget(self.expand_button, Qt.Corner.TopRightCorner)
        self._set_expanded(False)
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

    def _set_expanded(self, expanded: bool) -> None:
        """Expand: hide the folder / settings / summary / activity panels so the subjects and
        their outputs fill the tab; Restore brings them back. The run's progress stays."""
        self.top_panel.setVisible(not expanded)
        self.expand_button.setText("Restore ⤡" if expanded else "Expand ⤢")
        self.expand_button.setToolTip(
            "Show the folder, settings and activity panels again."
            if expanded
            else "Give the subjects and their outputs the whole height (hides the panels "
            "above; the run's progress stays)."
        )

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
                f"Re-read Docker's limits (they change after editing .wslconfig and "
                f"restarting WSL) and fill in Subjects, CPUs and Memory: about "
                f"{SUGGESTED_MEM_GB:g} GB and at least {MIN_CPUS_PER_SUBJECT} CPUs per "
                "subject, at most as many subjects as are ticked."
            )
        )
        self.suggest_button.clicked.connect(self._on_suggest)
        parallel_row.addWidget(self.suggest_button)
        form.addRow("Subjects:", parallel_row)
        self.cpus_spin = QSpinBox()
        self.cpus_spin.setRange(1, os.cpu_count() or 1)
        self.cpus_spin.setToolTip(
            "CPU cores C-PAC may use per subject (the total is this x Subjects)."
        )
        self.cpus_spin.valueChanged.connect(self._update_memory_label)
        form.addRow("CPUs per subject:", self.cpus_spin)
        self.mem_spin = QDoubleSpinBox()
        self.mem_spin.setRange(MIN_MEM_GB, 512.0)
        self.mem_spin.setSingleStep(1.0)
        self.mem_spin.setDecimals(0)
        self.mem_spin.setSuffix(" GB")
        self.mem_spin.setToolTip(
            wrap_tooltip(
                f"RAM C-PAC may use per subject (the total is this x Subjects). At least "
                f"{MIN_MEM_GB:g} GB: below that "
                "C-PAC refuses to start. Docker Desktop's own memory limit (Settings > "
                "Resources) must allow at least this much."
            )
        )
        self.mem_spin.valueChanged.connect(self._update_memory_label)
        form.addRow("Memory per subject:", self.mem_spin)
        layout.addLayout(form)
        self.memory_label = QLabel("Checking this computer's and Docker's memory...")
        self.memory_label.setWordWrap(True)
        style_secondary_label(self.memory_label)
        layout.addWidget(self.memory_label)

        self.docker_status = DockerStatus(
            "Clears Docker's file sharing when C-PAC fails with 'Input/output error'."
        )
        self.docker_status.action_started.connect(
            lambda action: self._log_activity(f"Docker: running 'docker desktop {action}'...")
        )
        self.docker_status.action_done.connect(self._on_docker_action_done)
        self.docker_label = self.docker_status.label
        self.docker_start_button = self.docker_status.start_button
        self.docker_restart_button = self.docker_status.restart_button
        layout.addWidget(self.docker_status)
        self.cpac_download = ImageDownload(
            CPAC_IMAGE,
            "the C-PAC image",
            "~15 GB",
            remove_note="The T1w defacer image (if set up) is built on C-PAC and keeps most of "
            "its layers, so less space is freed while it is there.",
            settings=self.settings,
        )
        self.cpac_download.finished.connect(self._on_cpac_downloaded)
        self.cpac_download.removed.connect(self._on_cpac_removed)
        layout.addWidget(self.cpac_download)
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

        self.hang_spin = QSpinBox()
        self.hang_spin.setRange(0, 24 * 60)
        self.hang_spin.setSingleStep(5)
        self.hang_spin.setSuffix(" min")
        self.hang_spin.setSpecialValueText("Off")
        self.hang_spin.setToolTip(
            wrap_tooltip(
                "C-PAC sometimes stops making progress without failing: it waits forever "
                "for a step that isn't running. A subject counts as hung when no step starts "
                "or finishes AND its container's CPU stays near idle for this long. Long real "
                "steps (registration: an hour or more) keep a CPU busy, so they don't count. "
                "Off: never check."
            )
        )
        self.hang_action_combo = QComboBox()
        for action, label in HANG_ACTIONS.items():
            self.hang_action_combo.addItem(label, action)
        self.hang_action_combo.setToolTip(
            wrap_tooltip(
                "Restart: stop the container and start it again; C-PAC picks up from its "
                "last finished step (up to the number of times given, then it counts as "
                "failed). Stop: free its CPU and memory and count it as failed (run it "
                "again later to resume). Only warn: note it in the activity log."
            )
        )
        self.restarts_spin = QSpinBox()
        self.restarts_spin.setRange(0, 10)
        self.restarts_spin.setPrefix("up to ")
        self.restarts_spin.setSuffix(" times")
        self.restarts_spin.setToolTip("How often one subject may be restarted after a hang.")
        self.hang_action_combo.currentIndexChanged.connect(self._update_hang_widgets)
        self.hang_spin.valueChanged.connect(self._update_hang_widgets)
        hang_row = QHBoxLayout()
        hang_row.addWidget(self.hang_spin)
        hang_row.addWidget(QLabel("then"))
        hang_row.addWidget(self.hang_action_combo, 1)
        hang_row.addWidget(self.restarts_spin)
        mode_form.addRow("If stuck for:", hang_row)

        self.keep_working_check = QCheckBox("Keep C-PAC's working folder after a subject finishes")
        self.keep_working_check.setToolTip(
            wrap_tooltip(
                "C-PAC's working folder holds every intermediate step (10+ GB per subject), "
                "inside Docker's own disk. It's what lets a stopped or hung subject resume. "
                "Off: deleted once the subject is fully preprocessed. On: kept, so a later "
                "rerun with other tasks can reuse the anatomical steps, at the cost of disk "
                "space on the drive Docker Desktop lives on."
            )
        )
        mode_form.addRow("", self.keep_working_check)
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
        """Large progress panel (gui/run_progress.py), shown only while a batch runs."""
        self.progress = RunProgress()
        self.run_title = self.progress.title_label
        self.running = self.progress.running  # one dict: the panel clears it, never replaces
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
        self.progress.add_button(self.stop_button)
        self.progress.add_button(self.stop_now_button)
        self.run_group = self.progress
        return self.progress

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
        saved = read_choices(self.settings)
        self.parallel_spin.setValue(saved.plan.parallel)
        self.cpus_spin.setValue(saved.plan.n_cpus)
        self.mem_spin.setValue(saved.plan.mem_gb)
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(saved.mode)))
        self.hang_spin.setValue(int(saved.hang.minutes))
        index = self.hang_action_combo.findData(saved.hang.action)
        self.hang_action_combo.setCurrentIndex(max(0, index))
        self.restarts_spin.setValue(saved.hang.max_restarts)
        self.keep_working_check.setChecked(saved.keep_working)
        self._update_hang_widgets()
        self.memory_probe = MemoryProbeThread()
        self.memory_probe.done.connect(self._on_memory_probed)
        self.memory_probe.start()
        self.docker_timer.start()
        self.unticked_tasks = saved.unticked_tasks
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
        self.folder_chosen.emit("bids", folder)
        self._fill_task_list()

    def _fill_task_list(self) -> None:
        """The BIDS folder's tasks, ticked unless unticked before (new tasks start ticked)."""
        self.task_list.blockSignals(True)
        self.task_list.clear()
        for label in bids_tasks(self.bids_folder) if self.bids_folder else []:
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
        self.folder_chosen.emit("output", folder)

    def use_folders(self, bids: Path | None, output: Path | None) -> None:
        """Show these folders (chosen on another tab); unchanged ones are left alone."""
        changed = False
        if output is not None and output != self.output_folder:
            self._set_output_folder(output)
            changed = True
        if bids is not None and bids != self.bids_folder:
            self._set_bids_folder(bids)
            changed = True
        if changed:
            self._refresh_roster()

    def reload_bids(self) -> None:
        """Read the BIDS folder's tasks and subjects again: another tab changed its contents
        (new conversions, renamed IDs, trimmed runs) without changing the folder itself."""
        self._fill_task_list()
        self._refresh_roster()

    def set_external_lock(self, reason: str) -> None:
        self.external_lock = reason
        self._update_buttons()

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
        return task_filter(self._all_task_labels(), self.unticked_tasks)

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
        save_unticked_tasks(self.settings, self.unticked_tasks)
        self._refresh_roster()

    # ---- roster ------------------------------------------------------------------------------

    def _subject_status(self, subject: str) -> str:
        return subject_status(
            self.bids_folder, self.output_folder, subject, self._tasks(), subject in self.running
        )

    def _tasks_to_do(self, subject: str) -> list[str]:
        """This subject's ticked tasks that aren't fully preprocessed yet."""
        return tasks_to_do(self.bids_folder, self.output_folder, subject, self._tasks())

    def _refresh_roster(self) -> None:
        selected = set(self._selected_subjects())
        task = self._tasks()
        ticked_any = bool(self._ticked_tasks())
        self.subjects = roster(self.bids_folder, self.output_folder, task, ticked_any)
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
        self._update_banner()
        self._refresh_summary()
        self._update_buttons()
        self._render_detail()

    def _update_banner(self) -> None:
        self.banner.set_messages(self._notices(bool(self._ticked_tasks())))

    def _notices(self, ticked_any: bool) -> list[str]:
        return notices(
            self.bids_folder,
            self.output_folder,
            self._all_task_labels(),
            ticked_any,
            self.cpac_missing,
        )

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
        text = summary_text(
            self.bids_folder,
            self.output_folder,
            self._tasks(),
            bool(self._ticked_tasks()),
            self.statuses,
        )
        self.summary_label.setText(text)

    def _selected_subjects(self) -> list[str]:
        model = self.subject_table.selectionModel()
        rows = sorted(index.row() for index in model.selectedRows()) if model else []
        return [self.subjects[row] for row in rows if row < len(self.subjects)]

    def _on_memory_probed(
        self,
        docker_gb: float | None,
        docker_cpus: int | None,
        docker_error: str,
        has_image: bool | None = None,  # None: unknown
    ) -> None:
        self.memory_probe.wait()
        self.docker_gb = docker_gb
        self.docker_cpus = docker_cpus
        self.docker_error = docker_error
        self.cpac_missing = has_image is False
        self.memory_probed = True
        self.cpac_download.set_present(has_image)
        self._update_banner()
        self._update_buttons()
        if self.suggest_pending:
            self.suggest_pending = False
            if docker_gb is not None:  # fresh limits: fill in again from them
                self._fill_in_suggestion()
        self._update_memory_label()
        self._update_docker_label()

    def _probe_docker(self) -> None:
        """Re-ask Docker whether it runs (and its memory), unless a check or start is busy."""
        if self.memory_probe.isRunning() or self.docker_control is not None:
            return
        self.memory_probe.start()

    @property
    def docker_control(self) -> DockerControlThread | None:
        """A Docker start / restart in progress, if any."""
        return self.docker_status.control

    def _update_docker_label(self) -> None:
        warning = (
            "Docker is running, but the C-PAC image isn't downloaded yet (below)."
            if self.cpac_missing
            else ""
        )
        self.docker_status.show_state(not self.docker_error, self.docker_error, warning)

    def _on_cpac_downloaded(self, error: str) -> None:
        if error:
            self._log_activity(f"C-PAC download stopped: {error}", True)
        else:
            self._log_activity(f"C-PAC image {CPAC_IMAGE} downloaded.")
        self._probe_docker()

    def _on_cpac_removed(self, error: str) -> None:
        if error:
            self._log_activity(f"C-PAC image not removed: {error}", True)
        else:
            self._log_activity(f"C-PAC image {CPAC_IMAGE} removed.")
        self._probe_docker()

    def _on_docker_action(self, action: str) -> None:
        self.docker_status.run(action)

    def _on_docker_action_done(self, action: str, error: str) -> None:
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
        return suggested_plan(self._docker_probe(), len(self._ticked_subjects()))

    def _docker_probe(self) -> DockerProbe:
        return DockerProbe(self.docker_gb, self.docker_cpus, self.docker_error)

    def _on_suggest(self) -> None:
        """Fill in from the limits known now, then again once Docker is re-asked: they
        go stale when WSL restarts with a new .wslconfig while this window is open."""
        self._fill_in_suggestion()
        self.suggest_pending = True
        self._probe_docker()

    def _fill_in_suggestion(self) -> None:
        plan = self._suggested_plan()
        self.parallel_spin.setValue(plan.parallel)
        self.cpus_spin.setValue(plan.n_cpus)
        self.mem_spin.setValue(plan.mem_gb)

    def _update_memory_label(self) -> None:
        if not self.memory_probed:
            return
        text, color = memory_text(
            self._plan(), self._docker_probe(), self.host_gb, len(self._ticked_subjects())
        )
        self.memory_label.setText(text)
        self.memory_label.setStyleSheet(f"color: {color};")

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
        self.cpac_download.set_locked("" if idle else "C-PAC is running")
        no_image = self.cpac_missing or self.cpac_download.busy
        self.process_button.setEnabled(
            idle
            and n_ticked > 0
            and self.output_folder is not None
            and not self.external_lock
            and not no_image
        )
        if self.external_lock:
            tip = f"Locked: {self.external_lock}."
        elif no_image:
            tip = "Download the C-PAC image first (button above)."
        else:
            tip = ""
        self.process_button.setToolTip(tip)
        self._update_memory_label()  # the suggestion depends on how many are ticked

    # ---- detail pane -------------------------------------------------------------------------

    def _render_detail(self) -> None:
        while self.outputs_layout.count():
            item = self.outputs_layout.takeAt(0)
            if item is not None and (widget := item.widget()) is not None:
                widget.hide()  # gone at once, not only when Qt gets round to deleting it
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
            stopped = self.statuses[subject] == "stopped"
            self.outputs_layout.addWidget(
                self._problem_group(failure_problem(self.output_folder, subject, stopped))
            )
        if self.statuses.get(subject) == "partial":
            self.outputs_layout.addWidget(
                self._problem_group(partial_problem(self._tasks_to_do(subject)))
            )
        if self.statuses.get(subject) == "renumbered" and self.bids_folder is not None:
            changes = session_changes(self.bids_folder, self.output_folder, subject)
            self.outputs_layout.addWidget(self._renumbered_group(subject, changes))
        if self.bids_folder is not None and self.statuses.get(subject) != "running":
            missing = missing_problem(self.bids_folder, self.output_folder, subject, self._tasks())
            if missing is not None:
                self.outputs_layout.addWidget(self._problem_group(missing))
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

    def _renumbered_group(self, subject: str, changes: list[SessionChange]) -> QGroupBox:
        """The subject's BIDS sessions were renumbered after it was preprocessed."""
        group = self._problem_group(renumbered_problem(changes))
        layout = group.layout()
        assert layout is not None
        rename = QPushButton("Rename output to the new session numbers...")
        rename.setToolTip(
            wrap_tooltip(
                "⚠ Renames this subject's C-PAC output and log files (e.g. ses-1 -> ses-2) "
                "so the existing results are labelled right, and clears its C-PAC working "
                "data (Docker must be running). The results themselves are unchanged: they "
                "were made from the same scans. The next C-PAC run of this subject still "
                "preprocesses every session again (C-PAC runs whole subjects, and the BIDS "
                "file names changed). Check the session list above first: renaming the "
                "wrong way round would mislabel results."
            )
        )
        rename.clicked.connect(partial(self._on_rename_outputs, subject, changes))
        layout.addWidget(rename)
        return group

    def _on_rename_outputs(self, subject: str, changes: list[SessionChange]) -> None:
        if self.bids_folder is None or self.output_folder is None:
            return
        answer = QMessageBox.question(self, "Rename output", rename_question(subject, changes))
        if answer != QMessageBox.StandardButton.Yes:
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            renamed = rename_subject(self.bids_folder, self.output_folder, subject, changes)
        except DockerUnavailableError as e:
            QMessageBox.warning(self, "Docker needed", f"Start Docker first: {e}")
            return
        except OSError as e:
            QMessageBox.warning(self, "Not renamed", str(e))
            return
        finally:
            QGuiApplication.restoreOverrideCursor()
        self._log_activity(
            f"sub-{subject}: renamed {renamed} output path(s) to the new session numbers"
        )
        self._refresh_roster()

    def _problem_group(self, problem: Problem) -> QGroupBox:
        group = QGroupBox(problem.title)
        group.setStyleSheet(accent_group_box_stylesheet(problem.color))
        layout = QVBoxLayout(group)
        for text in filter(None, (problem.text, "\n".join(problem.listing))):
            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setStyleSheet(f"color: {problem.color};")
            layout.addWidget(label)
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
        columns, rows = motion_rows(tables)
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
        self.log_text.setPlainText(log_text(self.output_folder, subject))
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
                scratch_question(subjects),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        plan = self._plan()
        if question := overload_question(plan, self._docker_probe(), len(subjects)):
            answer = QMessageBox.question(self, "More than Docker allows?", question)
            if answer != QMessageBox.StandardButton.Yes:
                return
        run = RunSettings(
            bids_root=self.bids_folder,
            output_root=self.output_folder,
            subjects=subjects,
            tasks=self._tasks(),
            n_cpus=plan.n_cpus,
            mem_gb=plan.mem_gb,
            mode=mode,
            parallel=min(plan.parallel, len(subjects)),
            hang=self._hang_policy(),
            keep_working=self.keep_working_check.isChecked(),
        )
        save_choices(self.settings, run, plan)
        self.output_folder.mkdir(parents=True, exist_ok=True)
        self.worker = PreprocessThread(run)
        self.worker.message.connect(self._log_activity)
        self.worker.subject_started.connect(self._on_subject_started)
        self.worker.step.connect(self._on_step)
        self.worker.subject_finished.connect(self._on_subject_finished)
        self.worker.batch_done.connect(self._on_batch_done)
        self._log_activity(start_text(run))
        self.progress.start(len(subjects), self._saved_estimate(), "Checking Docker...")
        self.stop_button.setEnabled(True)
        self.stop_now_button.setEnabled(True)
        self._update_buttons()
        self.worker.start()
        self.running_changed.emit(True)

    def _hang_policy(self) -> HangPolicy:
        return HangPolicy(
            float(self.hang_spin.value()),
            str(self.hang_action_combo.currentData()),
            self.restarts_spin.value(),
        )

    def _update_hang_widgets(self) -> None:
        checking = self.hang_spin.value() > 0
        self.hang_action_combo.setEnabled(checking)
        self.restarts_spin.setEnabled(
            checking and self.hang_action_combo.currentData() == "restart"
        )

    def _estimate_key(self) -> str:
        return estimate_key(self._tasks())

    def _saved_estimate(self) -> int | None:
        return saved_estimate(self.settings, self._tasks())

    def _on_stop(self) -> None:
        if self.worker is not None:
            self.worker.job.stop()
            self.stop_button.setEnabled(False)
            self._log_activity("Stopping once the running subject(s) finish...")

    def _on_stop_now(self) -> None:
        worker = self.worker
        if worker is None:
            return
        answer = QMessageBox.question(
            self,
            "Stop now?",
            stop_now_question(len(self.running)),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.stop_button.setEnabled(False)
        self.stop_now_button.setEnabled(False)
        self.progress.set_stage("Stopping...")
        self._log_activity("Stopping the running C-PAC container(s) now...")
        threading.Thread(target=worker.job.stop_now, daemon=True).start()  # docker takes ~10 s

    def _on_subject_started(self, _index: int, _total: int, subject: str) -> None:
        if not self.running:  # re-read: the last subject may have just saved a new count
            self.progress.estimate = self._saved_estimate()
        self.progress.subject_started(subject)
        self._set_status(subject, "running")
        self._update_live_line()

    def _on_step(self, subject: str, node: str, steps_done: int) -> None:
        self.progress.step(subject, steps_done, node)
        self._update_live_line()

    def _on_subject_finished(self, subject: str, steps_done: int, preprocessed: bool) -> None:
        if preprocessed and steps_done:
            self.settings.setValue(self._estimate_key(), steps_done)
        self.progress.subject_finished(subject)
        self._update_live_line()  # the thread's result line replaces this subject's
        self._set_status(subject, self._subject_status(subject))
        if self._selected_subjects() == [subject]:
            self._render_detail()

    def _on_batch_done(self, result: RunSummary | str) -> None:
        if self.worker is not None:
            self.worker.wait()
        self.worker = None
        self.progress.finish()
        self._set_live_line(None)
        self._update_buttons()
        self.running_changed.emit(False)
        if isinstance(result, str):
            QMessageBox.warning(self, "Preprocessing not started", result)
            self._log_activity(result, is_error=True)
        else:
            for line, is_error in done_lines(result):
                self._log_activity(line, is_error)
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
        lines = self.progress.batch.live_lines(self.progress.spinner)
        self._set_live_line("\n".join(lines) or None)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 -- Qt override
        if self.worker is None:
            self.docker_timer.stop()
            self.memory_probe.wait()  # seconds at most; a running QThread can't be destroyed
            self.docker_status.wait()
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
            stopped = self.worker.job.stop_now()
            if self.output_folder is not None:  # the worker threads won't get to it
                mark_all_stopped(self.output_folder, stopped)
        # A QThread destroyed while running aborts the process with a crash dialog, and the
        # worker may be blocked on docker for hours: leave at once.
        os._exit(0)


def main() -> None:
    """The toolbox (with its splash), opened on the Preprocess BIDS (C-PAC) tab."""
    from fmri_local_process.gui.launcher import preprocess

    preprocess()


if __name__ == "__main__":
    main()
