"""The MRIQC tab: run MRIQC on the BIDS folder before preprocessing, and look at its results.

* The output goes in its own folder, chosen here (remembered, like the other folders).
* Tick subjects and Run MRIQC: one Docker container per subject, then MRIQC's group step.
  Subjects already done are skipped unless 'Redo done subjects' is ticked; a subject whose
  BIDS files changed since (trimmed, reconverted) shows as stale and is run again.
* The results, grouped subject > session > run, show the key image quality metrics (IQMs);
  values that are outliers among the same kind of scan are highlighted. Select a run to see
  the toolbox's own look at the scan below (mriqc_views.py: drawn from the BIDS NIfTI, in the
  background, the last few kept), and rate it: 1 bad (discard), 2 unsure, 3 keep, with an
  optional comment (saved in the MRIQC output folder, toolbox_qc_ratings.tsv).
* Above it all, a banner says what is missing: no BIDS folder or scans, no output folder or
  results yet, Docker not installed / not running, or the MRIQC image not downloaded (with a
  button to fix each where it can be fixed here).
"""

import threading
from collections import OrderedDict
from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.gui.docker_controls import DOCKER_RECHECK_MS, DockerStatus
from fmri_local_process.gui.folder_panel import FolderPanel
from fmri_local_process.gui.image_download import ImageDownload
from fmri_local_process.gui.image_view import HelpToggle, ZoomableImageView
from fmri_local_process.gui.qt_common import (
    ERROR_COLOR,
    OK_COLOR,
    PROCESS_ACCENT_COLOR,
    PROCESS_ACCENT_HOVER_COLOR,
    RUNNING_COLOR,
    UNKNOWN_COLOR,
    WARNING_COLOR,
    NoticeBanner,
    primary_action_stylesheet,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.gui.run_progress import RunProgress
from fmri_local_process.gui.toolbox_state import QUALITY_CHECKING, VIEW_ONLY, ToolboxState
from fmri_local_process.processing import mriqc as mq
from fmri_local_process.processing import mriqc_views
from fmri_local_process.processing.cpac_docker import docker_resources, finished_node
from fmri_local_process.processing.review_mode import packaged_mriqc

OUTLIER_BACKGROUND = "#f5b7b1"
RATING_COLORS = {1: "#f1948a", 2: "#f9e79f", 3: "#abebc6"}  # black text reads on these
VIEW_CACHE_SIZE = 24  # drawn scans kept: going back to one is instant
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
SCAN_LIST_HEIGHT = 110
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


class ProbeThread(QThread):
    """Asks Docker whether it runs, has the MRIQC image, and its CPU / RAM limits."""

    done = Signal(str, str, object, object)  # state, message, CPUs or None, GB or None

    def run(self) -> None:
        state, message, docker = mq.docker_status()
        mem_gb, cpus = docker_resources(docker) if docker is not None else (None, None)
        self.done.emit(state, message, cpus, mem_gb)


class MriqcThread(QThread):
    """MRIQC for each subject in turn, then the group step."""

    message = Signal(str, bool)  # text, is_error
    subject_started = Signal(str)
    step = Signal(str, int, str)  # subject, pipeline steps finished, the last one's name
    subject_done = Signal(str, str)  # subject, why it failed ("" when done)
    stage = Signal(str)  # what runs between subjects, e.g. the group step
    all_done = Signal(str)  # summary

    def __init__(
        self,
        docker: Path,
        bids_root: Path,
        output_root: Path,
        subjects: list[str],
        n_procs: int,
        mem_gb: float,
        selection: mq.Selection = mq.EVERYTHING,
    ) -> None:
        super().__init__()
        self.docker, self.bids_root, self.output_root = docker, bids_root, output_root
        self.subjects, self.n_procs, self.mem_gb = subjects, n_procs, mem_gb
        self.selection = selection
        self.stop_requested = False
        self.current: str | None = None
        self.steps = 0

    def run(self) -> None:
        done, failed = [], []
        for number, subject in enumerate(self.subjects, start=1):
            if self.stop_requested:
                break
            self.current, self.steps = subject, 0
            self.message.emit(f"sub-{subject} ({number} of {len(self.subjects)}): started", False)
            self.subject_started.emit(subject)
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
                self.message.emit(f"sub-{subject}: done", False)
            else:
                failed.append(subject)
                self.message.emit(f"sub-{subject}: FAILED: {failure}", True)
            self.subject_done.emit(subject, failure or "")
        if done and not self.stop_requested:
            self.message.emit("Group step: summary tables and group reports...", False)
            self.stage.emit("Group step: summary tables and group reports...")
            log_path = self.output_root / mq.TOOLBOX_LOGS / "mriqc_group.txt"
            command = mq.build_group_command(self.docker, self.bids_root, self.output_root)
            try:
                code = mq.run_logged(command, log_path)
            except OSError as e:
                code, log_path = -1, Path(str(e))
            if code != 0:
                self.message.emit(f"Group step FAILED: {mq.explain_failure(log_path, code)}", True)
        stopped = " (stopped)" if self.stop_requested else ""
        self.all_done.emit(f"MRIQC finished{stopped}: {len(done)} done, {len(failed)} failed.")

    def on_line(self, line: str) -> None:
        """MRIQC is a nipype pipeline like C-PAC: count its finished steps the same way."""
        node = finished_node(line)
        if node is not None and self.current is not None:
            self.steps += 1
            self.step.emit(self.current, self.steps, node.rsplit(".", 1)[-1])

    def stop(self) -> None:
        """Start no more subjects and stop the running container (in the background: docker
        takes up to ~10 s)."""
        self.stop_requested = True
        if self.current is not None:
            name = mq.container_name(self.current)
            threading.Thread(
                target=mq.stop_container, args=(self.docker, name), daemon=True
            ).start()


class RenderThread(QThread):
    """Draws one scan's view (reading a whole BOLD series takes a second or more)."""

    done = Signal(object, object, str)  # key, QImage or None, why not

    def __init__(self, key: tuple, path: Path, modality: str) -> None:
        super().__init__()
        self.key, self.path, self.modality = key, path, modality

    def run(self) -> None:
        try:
            figure = mriqc_views.run_figure(self.path, self.modality)
        except (mriqc_views.ViewError, MemoryError) as e:
            self.done.emit(self.key, None, str(e))
            return
        # Agg draws off the GUI thread; a QImage (unlike a QPixmap) may be made here.
        figure.set_dpi(100)
        canvas = FigureCanvasAgg(figure)
        canvas.draw()
        width, height = canvas.get_width_height()
        image = QImage(canvas.buffer_rgba(), width, height, QImage.Format.Format_RGBA8888)
        self.done.emit(self.key, image.copy(), "")  # copy: the buffer dies with the canvas


class MriqcPage(QWidget):
    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        # A review package holding MRIQC results: show them, never run MRIQC.
        self.packaged_results = packaged_mriqc()
        self.seen: tuple = ()  # (generation, BIDS folder, output folder) last shown
        self.subjects: list[str] = []
        self.statuses: dict[str, str] = {}
        self.unticked: set[str] = set()
        # Untick-list rather than tick-list (as on Preprocess): new tasks start ticked.
        saved = str(state.settings.value(UNTICKED_SCANS_KEY, "") or "")
        self.unticked_scans: set[str] = set(filter(None, saved.split(",")))
        self.runs: list[mq.RunIqms] = []
        self.unreadable: list[str] = []
        self.docker_state = ""  # "" until the probe answers
        self.docker_message = ""
        self.docker: Path | None = None
        self.docker_cpus: int | None = None  # Docker's limits, from the last probe
        self.docker_gb: float | None = None
        self.suggest_pending = False  # 'Suggest' waits for Docker's fresh limits
        self.probe: ProbeThread | None = None
        self.worker: MriqcThread | None = None
        # Drawing a scan's view: one at a time; a run clicked meanwhile waits as 'wanted'
        # (only the last one clicked), so quick clicking never piles up work.
        self.renderer: RenderThread | None = None
        self.wanted: tuple | None = None  # (key, path, modality) to draw next
        self.views: OrderedDict[tuple, QPixmap | str] = OrderedDict()  # key: (path, mtime)
        self.shown_key: tuple | None = None
        self.ratings: dict[str, mq.QcRating] = {}
        self.runs_by_name: dict[str, mq.RunIqms] = {}
        self.formulas_dialogs: dict[str, QDialog] = {}  # per modality, made on first use

        layout = QVBoxLayout(self)
        # Everything above the results, in one widget so 'Expand' can hide it (as on Preprocess).
        self.top_panel = QWidget()
        top_layout = QVBoxLayout(self.top_panel)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(
            FolderPanel(
                state,
                "bids",
                "BIDS Folder (input)",
                "The BIDS folder MRIQC checks (chosen on the Setup & Convert to BIDS tab). "
                "It is only read.",
                home_base=True,
                can_browse=False,
            ),
            1,
        )
        top.addWidget(
            FolderPanel(
                state,
                "mriqc",
                "MRIQC output",
                "Where MRIQC writes its reports and quality metrics: a folder of its own, "
                "outside the BIDS and C-PAC output folders.",
                border_color=PROCESS_ACCENT_COLOR,
            ),
            1,
        )
        top.addWidget(self._build_run_box(), 1)
        top_layout.addLayout(top)

        self.banner = NoticeBanner()
        top_layout.addWidget(self.banner)
        self.download = ImageDownload(
            mq.MRIQC_IMAGE, "the MRIQC image", "~5 GB", settings=state.settings
        )
        self.download.finished.connect(self.on_downloaded)
        self.download.removed.connect(self.on_removed)
        top_layout.addWidget(self.download)
        top_layout.addWidget(self.help.box)
        layout.addWidget(self.top_panel)
        self.progress = RunProgress("Running MRIQC")  # the same panel as C-PAC's
        self.stop_button = QPushButton("Stop (the running subject is redone next time)")
        self.stop_button.setToolTip(
            wrap_tooltip(
                "Stops the running MRIQC container at once and starts no more subjects. MRIQC "
                "can't resume a subject: the one it was on starts afresh next time."
            )
        )
        self.stop_button.clicked.connect(self.on_stop)
        self.progress.add_button(self.stop_button)
        layout.addWidget(self.progress)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.subjects_panel = self._build_subjects()
        splitter.addWidget(self.subjects_panel)
        splitter.addWidget(self._build_results())
        splitter.setSizes([380, 1000])
        layout.addWidget(splitter, 1)
        self.set_expanded(False)

        # Hidden, it refreshes when next shown (show_page compares the folders it last saw).
        state.folder_changed.connect(lambda _key: self.refresh() if self.isVisible() else None)
        state.busy_changed.connect(self.update_controls)
        self.probed = False  # Docker is first asked when the tab is first shown

    # ---- building ------------------------------------------------------------------------------

    def _build_run_box(self) -> QGroupBox:
        box = QGroupBox("Run MRIQC")
        layout = QVBoxLayout(box)
        form = QFormLayout()
        self.cpus = QSpinBox()
        self.cpus.setRange(1, 128)
        self.cpus.setValue(int(self.saved_number("mriqc_cpus", 4)))
        self.cpus.setToolTip("CPU cores MRIQC may use (at most what Docker Desktop allows).")
        self.cpus.valueChanged.connect(lambda n: self.state.settings.setValue("mriqc_cpus", n))
        form.addRow("CPUs:", self.cpus)
        self.memory = QDoubleSpinBox()
        self.memory.setRange(mq.MIN_MEM_GB, 512.0)
        self.memory.setDecimals(0)
        self.memory.setSuffix(" GB")
        self.memory.setValue(self.saved_number("mriqc_mem_gb", 8))
        self.memory.setToolTip("Memory MRIQC may use (at most what Docker Desktop allows).")
        self.memory.valueChanged.connect(
            lambda gb: self.state.settings.setValue("mriqc_mem_gb", gb)
        )
        memory_row = QHBoxLayout()
        memory_row.addWidget(self.memory, 1)
        self.suggest_button = QPushButton("Suggest")
        self.suggest_button.setToolTip(
            wrap_tooltip(
                "Fill in CPUs and memory from what Docker Desktop allows: all its CPUs (one "
                "subject runs at a time) and its memory less 2 GB for Docker itself. Asks "
                "Docker again first, in case its limits changed."
            )
        )
        self.suggest_button.clicked.connect(self.on_suggest)
        memory_row.addWidget(self.suggest_button)
        form.addRow("Memory:", memory_row)
        layout.addLayout(form)
        self.limits_label = QLabel()
        style_secondary_label(self.limits_label)
        layout.addWidget(self.limits_label)
        self.docker_status = DockerStatus()  # the same line and buttons as on Preprocess
        self.docker_status.action_started.connect(
            lambda action: self.log(f"Docker: running 'docker desktop {action}'...")
        )
        self.docker_status.action_done.connect(self.on_docker_action_done)
        layout.addWidget(self.docker_status)
        # Re-ask now and then, so the line follows Docker started or stopped elsewhere.
        self.docker_timer = QTimer(self)
        self.docker_timer.setInterval(DOCKER_RECHECK_MS)
        self.docker_timer.timeout.connect(self.recheck_docker)
        self.cpus.valueChanged.connect(self.update_limits_label)
        self.memory.valueChanged.connect(self.update_limits_label)
        # Off at every start, never remembered: rerunning done subjects is a one-off choice.
        self.redo_box = QCheckBox("Redo done subjects")
        self.redo_box.setToolTip("Run ticked subjects again even when their results are current.")
        layout.addWidget(self.redo_box)
        buttons = QHBoxLayout()
        self.help = HelpToggle("Help ?", "What MRIQC does, and how to read the table.")
        self.help.set_text(HELP_TEXT)
        buttons.addWidget(self.help.button)
        buttons.addStretch(1)
        self.run_button = QPushButton("Run MRIQC")
        self.run_button.setStyleSheet(
            primary_action_stylesheet(PROCESS_ACCENT_COLOR, PROCESS_ACCENT_HOVER_COLOR)
        )
        self.run_button.clicked.connect(self.on_run)
        buttons.addWidget(self.run_button)
        layout.addLayout(buttons)
        self.run_status = QLabel()
        self.run_status.setWordWrap(True)
        style_secondary_label(self.run_status)
        layout.addWidget(self.run_status)
        self.run_box = box
        return box

    def set_expanded(self, expanded: bool) -> None:
        """Expand: the results table and the scan view fill the tab (the folder, run and subject
        panels and the notices are hidden; a running batch's progress stays). Restore: back."""
        self.top_panel.setVisible(not expanded)
        self.subjects_panel.setVisible(not expanded)
        self.expand_button.setText("Restore ⤡" if expanded else "Expand ⤢")
        self.expand_button.setToolTip(
            "Show the folder, run and subject panels again."
            if expanded
            else "Give the results and the scan view the whole tab (hides the panels above "
            "and the subject list; a run's progress stays)."
        )

    def saved_number(self, key: str, default: float) -> float:
        try:
            return float(str(self.state.settings.value(key, default)))
        except ValueError:
            return default

    def _build_subjects(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        scans_row = QHBoxLayout()
        scans_row.addWidget(QLabel("Scans to check"))
        scans_row.addStretch(1)
        for text, ticked in (("All", True), ("None", False)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, on=ticked: self.tick_all_scans(on))
            scans_row.addWidget(button)
        layout.addLayout(scans_row)
        self.scan_list = QListWidget()
        self.scan_list.setMaximumHeight(SCAN_LIST_HEIGHT)
        self.scan_list.setToolTip(
            wrap_tooltip(
                "Tick the scans MRIQC checks: the T1w and each BOLD task. Subjects, their "
                "status and what runs follow the ticks. Remembered."
            )
        )
        self.scan_list.itemChanged.connect(self.on_scan_ticked)
        layout.addWidget(self.scan_list)
        row = QHBoxLayout()
        row.addWidget(QLabel("Subjects to run"))
        row.addStretch(1)
        for text, ticked in (("Tick all", True), ("Tick none", False)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, on=ticked: self.tick_all(on))
            row.addWidget(button)
        layout.addLayout(row)
        self.subject_table = QTableWidget(0, 4)
        self.subject_table.setHorizontalHeaderLabels(["Subject", "Sessions", "Scans", "Status"])
        self.subject_table.verticalHeader().setVisible(False)
        self.subject_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.subject_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.subject_table.horizontalHeader().setStretchLastSection(True)
        self.subject_table.itemChanged.connect(self.on_tick_changed)
        self.subject_table.itemSelectionChanged.connect(self.on_subject_selected)
        layout.addWidget(self.subject_table, 2)
        layout.addWidget(QLabel("Activity"))
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setFont(QFont("Courier New"))
        self.activity.setPlaceholderText("Run MRIQC: progress and problems appear here.")
        layout.addWidget(self.activity, 1)
        return panel

    def _build_results(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Vertical)
        table_panel = self.table_panel = QWidget()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Scans:"))
        self.modality_box = QComboBox()
        for modality in mq.MODALITIES:
            self.modality_box.addItem(MODALITY_TEXT[modality], modality)
        self.modality_box.currentIndexChanged.connect(self.load_results)
        row.addWidget(self.modality_box)
        self.flagged_only = QCheckBox("Only runs with outliers")
        self.flagged_only.toggled.connect(self.fill_tree)
        row.addWidget(self.flagged_only)
        row.addStretch(1)
        self.formulas_button = QPushButton("Metric formulas...")
        self.formulas_button.setToolTip(
            wrap_tooltip(
                "How every metric in the table is calculated (MRIQC's published definitions), "
                "typical values where the literature gives them, and references."
            )
        )
        self.formulas_button.clicked.connect(self.on_show_formulas)
        row.addWidget(self.formulas_button)
        self.expand_button = QPushButton()
        self.expand_button.setCheckable(True)
        self.expand_button.toggled.connect(self.set_expanded)
        row.addWidget(self.expand_button)
        table_layout.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setAlternatingRowColors(True)
        self.tree.itemSelectionChanged.connect(self.on_run_selected)
        table_layout.addWidget(self.tree, 1)
        splitter.addWidget(table_panel)

        view_panel = QWidget()
        view_layout = QVBoxLayout(view_panel)
        view_layout.setContentsMargins(0, 0, 0, 0)
        view_layout.addLayout(self._build_rating_row())
        self.scan_view = ZoomableImageView()
        for text, step, tip in (
            ("◀ Prev run", -1, "The run above in the table (keeps the view expanded)."),
            ("Next run ▶", 1, "The run below in the table (keeps the view expanded)."),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, s=step: self.step_run(s))
            self.scan_view.toolbar.addWidget(button)
        self.view_expand_button = QPushButton()
        self.view_expand_button.setCheckable(True)
        self.view_expand_button.toggled.connect(self.set_view_expanded)
        self.scan_view.toolbar.addWidget(self.view_expand_button)
        view_layout.addWidget(self.scan_view, 1)
        splitter.addWidget(view_panel)
        splitter.setSizes([340, 560])
        self.show_note("Select a run above to see the scan here and rate it.")
        self.set_view_expanded(False)
        return splitter

    def set_view_expanded(self, expanded: bool) -> None:
        """Expand view: the scan and its rating row fill the tab (the table and every panel
        hidden; Prev / Next run still go through the runs). Restore: back as it was."""
        self.table_panel.setVisible(not expanded)
        panels = not expanded and not self.expand_button.isChecked()
        self.top_panel.setVisible(panels)
        self.subjects_panel.setVisible(panels)
        self.view_expand_button.setText("Restore ⤡" if expanded else "Expand view ⤢")
        self.view_expand_button.setToolTip(
            "Show the results table and the panels again."
            if expanded
            else "Give the scan the whole tab: hides the table and the panels. Rate it, then "
            "Next run."
        )
        if self.scan_view.fit:
            QTimer.singleShot(0, self.scan_view.fit_to_window)  # once the new size is laid out

    def run_items(self) -> list[QTreeWidgetItem]:
        """The run rows in table order (not the subject and session rows)."""
        items = []
        for top in range(self.tree.topLevelItemCount()):
            subject = self.tree.topLevelItem(top)
            for s in range(subject.childCount() if subject else 0):
                session = subject.child(s) if subject else None
                for r in range(session.childCount() if session else 0):
                    run = session.child(r) if session else None
                    if run is not None:
                        items.append(run)
        return items

    def step_run(self, step: int) -> None:
        """Select the previous / next run (the first one when none is selected)."""
        items = self.run_items()
        if not items:
            return
        current = self.tree.currentItem()
        index = items.index(current) + step if current in items else 0
        self.tree.setCurrentItem(items[max(0, min(index, len(items) - 1))])

    def _build_rating_row(self) -> QHBoxLayout:
        """QC rating: [1 bad: discard] [2 unsure] [3 keep]   Comment: [...........]"""
        row = QHBoxLayout()
        self.rating_title = QLabel("QC rating:")
        row.addWidget(self.rating_title)
        self.rating_group = QButtonGroup(self)
        for value, meaning in mq.RATINGS.items():
            button = QPushButton(f"{value} {meaning}")
            button.setCheckable(True)
            button.setStyleSheet(
                f"QPushButton:checked {{ background-color: {RATING_COLORS[value]}; "
                "color: black; font-weight: bold; }"
            )
            self.rating_group.addButton(button, value)
            row.addWidget(button)
        self.rating_group.idClicked.connect(self.on_rating_clicked)
        row.addWidget(QLabel("Comment:"))
        self.comment = QLineEdit()
        self.comment.setPlaceholderText("optional; saved when you press Enter or click away")
        self.comment.editingFinished.connect(self.save_current_rating)
        row.addWidget(self.comment, 1)
        self.rating_note = QLabel()
        style_secondary_label(self.rating_note)
        row.addWidget(self.rating_note)
        self.set_rating_enabled(False)
        return row

    # ---- refreshing ----------------------------------------------------------------------------

    def results_only(self) -> bool:
        """Only show results: a view-only package, or one that came with MRIQC's results."""
        return VIEW_ONLY in self.state.busy or self.packaged_results

    def show_page(self) -> None:
        seen = (self.state.generation, self.state.folder("bids"), self.state.folder("mriqc"))
        if seen != self.seen:
            self.refresh()
        if not self.probed and not self.results_only():
            self.probed = True
            self.check_docker()
            self.docker_timer.start()

    def refresh(self) -> None:
        bids_root, output_root = self.state.folder("bids"), self.state.folder("mriqc")
        self.seen = (self.state.generation, bids_root, output_root)
        self.fill_scan_list()
        self.fill_subjects()
        self.load_results()

    # ---- which scans ---------------------------------------------------------------------------

    def fill_scan_list(self) -> None:
        bids_root = self.state.folder("bids")
        keys = []
        if bids_root is not None and bids_root.is_dir():
            keys = [T1W_SCAN] if mq.has_t1w(bids_root) else []
            keys += [f"task-{task}" for task in mq.bids_tasks(bids_root)]
        self.scan_list.blockSignals(True)
        self.scan_list.clear()
        for key in keys:
            text = "T1w (anatomical)" if key == T1W_SCAN else f"{key} (BOLD)"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            ticked = key not in self.unticked_scans
            item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
            self.scan_list.addItem(item)
        self.scan_list.blockSignals(False)

    def scan_keys(self) -> list[str]:
        items = (self.scan_list.item(row) for row in range(self.scan_list.count()))
        return [str(item.data(Qt.ItemDataRole.UserRole)) for item in items if item is not None]

    def selection(self) -> mq.Selection:
        """The ticked scans; every BOLD task ticked counts as all tasks (new ones too)."""
        tasks = [key for key in self.scan_keys() if key.startswith("task-")]
        ticked = [key.removeprefix("task-") for key in tasks if key not in self.unticked_scans]
        return mq.Selection(
            t1w=T1W_SCAN not in self.unticked_scans,
            tasks=None if len(ticked) == len(tasks) else tuple(ticked),
        )

    def on_scan_ticked(self, item: QListWidgetItem) -> None:
        key = str(item.data(Qt.ItemDataRole.UserRole))
        if item.checkState() == Qt.CheckState.Checked:
            self.unticked_scans.discard(key)
        else:
            self.unticked_scans.add(key)
        self.save_scan_ticks()
        self.fill_subjects()

    def tick_all_scans(self, on: bool) -> None:
        self.unticked_scans = set() if on else set(self.scan_keys())
        self.save_scan_ticks()
        self.fill_scan_list()
        self.fill_subjects()

    def save_scan_ticks(self) -> None:
        self.state.settings.setValue(UNTICKED_SCANS_KEY, ",".join(sorted(self.unticked_scans)))

    # ---- subjects ------------------------------------------------------------------------------

    def fill_subjects(self) -> None:
        bids_root, output_root = self.state.folder("bids"), self.state.folder("mriqc")
        selection = self.selection()
        self.subjects = (
            mq.bids_subjects(bids_root, selection) if bids_root and bids_root.is_dir() else []
        )
        self.statuses = {
            subject: mq.subject_state(bids_root, output_root, subject, selection)
            for subject in self.subjects
            if bids_root is not None
        }
        table = self.subject_table
        table.blockSignals(True)
        table.setRowCount(len(self.subjects))
        for row, subject in enumerate(self.subjects):
            scans = mq.bids_scans(bids_root, subject, selection) if bids_root else []
            name = QTableWidgetItem(f"sub-{subject}")
            name.setFlags(name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            ticked = subject not in self.unticked
            name.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
            table.setItem(row, 0, name)
            sessions = {scan.session for scan in scans}
            table.setItem(row, 1, QTableWidgetItem(str(len(sessions - {""}) or "-")))
            table.setItem(row, 2, QTableWidgetItem(str(len(scans))))
            table.setItem(row, 3, self.status_item(self.statuses.get(subject, mq.NOT_RUN)))
        table.blockSignals(False)
        table.resizeColumnsToContents()
        self.update_controls()

    @staticmethod
    def status_item(status: str, note: str = "") -> QTableWidgetItem:
        item = QTableWidgetItem(note or status)
        item.setForeground(QColor(STATUS_COLOR.get(status, ERROR_COLOR)))
        if status == mq.STALE:
            item.setToolTip("BIDS files changed since MRIQC ran (trimmed or reconverted): rerun.")
        return item

    def load_results(self) -> None:
        output_root = self.state.folder("mriqc")
        modality = str(self.modality_box.currentData())
        if output_root is not None and output_root.is_dir():
            self.runs, self.unreadable = mq.collect_iqms(
                output_root, modality, self.state.folder("bids")
            )
        else:
            self.runs, self.unreadable = [], []
        self.ratings = mq.read_ratings(output_root) if output_root is not None else {}
        self.runs_by_name = {run.name: run for run in self.runs}
        self.fill_tree()
        self.update_notices()

    def fill_tree(self) -> None:
        modality = str(self.modality_box.currentData())
        metrics = mq.METRICS[modality]
        flags = mq.flag_outliers(self.runs)
        selected = self.selected_run()
        self.tree.blockSignals(True)  # clearing would select nothing: keep the view as it is
        self.tree.clear()
        self.tree.setColumnCount(FIRST_METRIC_COLUMN + len(metrics))
        self.tree.setHeaderLabels(
            ["Subject / session / run", "QC rating", "Flags", *(m.label for m in metrics)]
        )
        header = self.tree.headerItem()
        for column, metric in enumerate(metrics, start=FIRST_METRIC_COLUMN):
            better = f" ({metric.better} is better)" if metric.better else ""
            header.setToolTip(column, wrap_tooltip(metric.help + better))
        header.setToolTip(QC_COLUMN, "Your verdict on the run: 1 bad (discard), 2 unsure, 3 keep.")
        header.setToolTip(FLAGS_COLUMN, "How many of the run's metrics are outliers (or 'stale').")
        black = QColor("black")
        reselect: QTreeWidgetItem | None = None
        for subject, sessions in mq.by_subject_session(self.runs).items():
            subject_item = QTreeWidgetItem([f"sub-{subject}"])
            bold_font = subject_item.font(0)
            bold_font.setBold(True)
            subject_item.setFont(0, bold_font)
            subject_flags = 0
            for session, runs in sessions.items():
                session_item = QTreeWidgetItem([f"ses-{session}" if session else "(no session)"])
                session_flags = 0
                for run in runs:
                    flagged = flags.get(run.name, set())
                    if self.flagged_only.isChecked() and not flagged:
                        continue
                    session_flags += len(flagged)
                    note = f"{len(flagged)} outlier(s)" if flagged else ""
                    if run.state == mq.STALE:
                        note = ", ".join(filter(None, [note, "stale"]))
                    run_item = QTreeWidgetItem([run.label, "", note])
                    run_item.setData(0, Qt.ItemDataRole.UserRole, run.name)
                    run_item.setToolTip(0, run.name)
                    self.show_rating_in(run_item, self.ratings.get(run.name))
                    if run.name == selected:
                        reselect = run_item
                    if run.state == mq.STALE:
                        run_item.setBackground(FLAGS_COLUMN, QColor(STALE_BACKGROUND))
                        run_item.setForeground(FLAGS_COLUMN, black)
                        run_item.setToolTip(
                            FLAGS_COLUMN, "The BIDS file changed since MRIQC ran: rerun."
                        )
                    for column, metric in enumerate(metrics, start=FIRST_METRIC_COLUMN):
                        value = run.values.get(metric.key)
                        run_item.setText(column, "" if value is None else f"{value:.3g}")
                        if metric.key in flagged:
                            run_item.setBackground(column, QColor(OUTLIER_BACKGROUND))
                            run_item.setForeground(column, black)
                            run_item.setToolTip(
                                column, "Outlier among the runs of this kind of scan."
                            )
                    session_item.addChild(run_item)
                if session_item.childCount():
                    session_item.setText(
                        FLAGS_COLUMN, f"{session_flags} outlier(s)" if session_flags else ""
                    )
                    subject_item.addChild(session_item)
                subject_flags += session_flags
            if subject_item.childCount():
                subject_item.setText(
                    FLAGS_COLUMN, f"{subject_flags} outlier(s)" if subject_flags else ""
                )
                self.tree.addTopLevelItem(subject_item)
        self.tree.expandAll()
        for column in range(self.tree.columnCount()):
            self.tree.resizeColumnToContents(column)
        if reselect is not None:
            self.tree.setCurrentItem(reselect)
        self.tree.blockSignals(False)
        self.on_run_selected()

    @staticmethod
    def show_rating_in(item: QTreeWidgetItem, rating: mq.QcRating | None) -> None:
        item.setText(QC_COLUMN, rating.text if rating else "")
        tip = ""
        if rating is not None:
            who = ", ".join(filter(None, [rating.rater, rating.date.replace("T", " ")]))
            tip = "\n".join(filter(None, [rating.comment, f"({who})" if who else ""]))
        item.setToolTip(QC_COLUMN, tip)
        if rating is not None and rating.rating in RATING_COLORS:
            item.setBackground(QC_COLUMN, QColor(RATING_COLORS[rating.rating]))
            item.setForeground(QC_COLUMN, QColor("black"))
        else:
            item.setData(QC_COLUMN, Qt.ItemDataRole.BackgroundRole, None)
            item.setData(QC_COLUMN, Qt.ItemDataRole.ForegroundRole, None)
        if rating is not None and rating.comment and not rating.rating:
            item.setText(QC_COLUMN, "comment only")

    def notices(self) -> list[str]:
        """What is missing, most basic first."""
        bids_root, output_root = self.state.folder("bids"), self.state.folder("mriqc")
        messages = []
        if bids_root is None:
            messages.append("No BIDS folder chosen: choose it on the Setup & Convert to BIDS tab.")
        elif not self.scan_keys():
            messages.append(
                f"No BOLD or T1w scans in {bids_root}: convert the DICOMs on the Setup & "
                "Convert to BIDS tab first."
            )
        elif self.selection().empty or not self.subjects:
            messages.append("No scans ticked: tick the scans MRIQC should check (left).")
        if output_root is None:
            messages.append(
                "No MRIQC output folder chosen: Browse for one above; MRIQC writes its reports "
                "there."
            )
        elif not self.runs and not mq.has_results(output_root):
            todo = "" if self.results_only() else " yet: tick subjects and Run MRIQC"
            messages.append(f"No MRIQC results in {output_root}{todo}.")
        elif not self.runs:
            modality = MODALITY_TEXT[str(self.modality_box.currentData())]
            messages.append(f"No MRIQC results for {modality} scans in {output_root}.")
        if self.unreadable:
            messages.append(
                f"{len(self.unreadable)} MRIQC result file(s) couldn't be read: "
                + "; ".join(self.unreadable[:3])
            )
        if self.docker_message and not self.results_only():
            messages.append(self.docker_message)
        return messages

    def update_notices(self) -> None:
        self.banner.set_messages(self.notices())
        present = {mq.READY: True, mq.IMAGE_MISSING: False}.get(self.docker_state)
        self.download.set_present(None if self.results_only() else present)
        self.docker_status.setVisible(not self.results_only())
        self.docker_status.show_state(*DOCKER_LINE.get(self.docker_state, (None, "", "")))

    def ticked_subjects(self) -> list[str]:
        return [s for s in self.subjects if s not in self.unticked]

    def to_run(self) -> list[str]:
        redo = self.redo_box.isChecked()
        return [s for s in self.ticked_subjects() if redo or self.statuses.get(s) != mq.DONE]

    def update_controls(self) -> None:
        view_only = self.results_only()
        self.run_box.setVisible(not view_only)
        running = self.worker is not None
        reason = self.state.lock_reason(QUALITY_CHECKING)
        ready = (
            not running
            and not reason
            and self.docker_state == mq.READY
            and self.state.folder("mriqc") is not None
            and bool(self.to_run())
        )
        self.run_button.setEnabled(ready)
        self.scan_list.setEnabled(not running)
        self.download.set_locked("MRIQC is running" if running else "")
        if not running:
            if reason:
                self.run_status.setText(f"Locked: {reason}.")
            elif self.docker_state == "":
                self.run_status.setText("Checking Docker...")
            elif self.ticked_subjects() and not self.to_run():
                self.run_status.setText("Every ticked subject is done (tick 'Redo' to rerun).")
            else:
                self.run_status.setText(f"{len(self.to_run())} subject(s) to run.")

    # ---- Docker --------------------------------------------------------------------------------

    def check_docker(self, forget: bool = True) -> None:
        """Ask Docker again (forget: show 'Checking Docker...' meanwhile)."""
        if self.probe is not None:
            return
        if forget:
            self.docker_state, self.docker_message = "", ""
        self.probe = ProbeThread()
        self.probe.done.connect(self.on_probed)
        self.probe.start()
        self.update_notices()
        self.update_controls()

    def on_probed(self, state: str, message: str, cpus: int | None, mem_gb: float | None) -> None:
        if self.probe is not None:
            self.probe.wait()
        self.probe = None
        self.docker_state, self.docker_message = state, message
        self.docker_cpus, self.docker_gb = cpus, mem_gb
        if cpus is not None:
            self.cpus.setMaximum(cpus)
        if mem_gb is not None:
            self.memory.setMaximum(mq.suggest_resources(cpus, mem_gb)[1])
        first_time = self.state.settings.value("mriqc_cpus") is None
        if self.suggest_pending or (first_time and cpus is not None):
            self.fill_in_suggestion()
        self.suggest_pending = False
        self.update_limits_label()
        self.update_notices()
        self.update_controls()

    def on_suggest(self) -> None:
        """Fill in from the limits known now, then again once Docker answers afresh."""
        self.fill_in_suggestion()
        self.suggest_pending = True
        self.check_docker()

    def fill_in_suggestion(self) -> None:
        cpus, mem_gb = mq.suggest_resources(self.docker_cpus, self.docker_gb)
        self.cpus.setValue(cpus)
        self.memory.setValue(mem_gb)

    def update_limits_label(self) -> None:
        if self.docker_cpus is None and self.docker_gb is None:
            self.limits_label.setText("Docker's CPU and memory limits not known yet.")
            return
        cpus = f"{self.docker_cpus} CPUs" if self.docker_cpus else "? CPUs"
        memory = f"{self.docker_gb:.1f} GB" if self.docker_gb else "? GB"
        suggested = mq.suggest_resources(self.docker_cpus, self.docker_gb)
        note = (
            ""
            if (self.cpus.value(), self.memory.value()) == suggested
            else f" Suggested: {suggested[0]} CPUs, {suggested[1]:g} GB."
        )
        self.limits_label.setText(f"Docker allows {cpus}, {memory}.{note}")

    def recheck_docker(self) -> None:
        """The timer's quiet re-check: not while Docker is being started or restarted."""
        if not self.docker_status.busy:
            self.check_docker(forget=False)

    def on_docker_action_done(self, action: str, error: str) -> None:
        if error:
            self.log(f"Docker {action} failed: {error}", error=True)
        else:
            self.log(f"Docker {action}: done.")
        self.check_docker()

    def on_downloaded(self, error: str) -> None:
        self.log(error or "MRIQC image downloaded.", error=bool(error))
        self.check_docker()

    def on_removed(self, error: str) -> None:
        self.log(
            f"MRIQC image not removed: {error}" if error else "MRIQC image removed.", bool(error)
        )
        self.check_docker()

    # ---- running -------------------------------------------------------------------------------

    def on_tick_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        subject = item.text().removeprefix("sub-")
        if item.checkState() == Qt.CheckState.Checked:
            self.unticked.discard(subject)
        else:
            self.unticked.add(subject)
        self.update_controls()

    def tick_all(self, on: bool) -> None:
        self.unticked = set() if on else set(self.subjects)
        self.fill_subjects()

    def on_run(self) -> None:
        bids_root, output_root = self.state.folder("bids"), self.state.folder("mriqc")
        subjects = self.to_run()
        if bids_root is None or output_root is None or not subjects or self.worker is not None:
            return
        _state, message, docker = mq.docker_status()
        if docker is None or _state != mq.READY:
            self.docker_state, self.docker_message = _state, message
            self.update_notices()
            self.update_controls()
            return
        selection = self.selection()
        self.worker = MriqcThread(
            docker,
            bids_root,
            output_root,
            subjects,
            self.cpus.value(),
            self.memory.value(),
            selection,
        )
        self.worker.message.connect(self.log)
        self.worker.subject_started.connect(self.on_subject_started)
        self.worker.step.connect(self.on_step)
        self.worker.subject_done.connect(self.on_subject_done)
        self.worker.stage.connect(self.progress.set_stage)
        self.worker.all_done.connect(self.on_all_done)
        self.state.set_busy(QUALITY_CHECKING, True)
        self.log(f"MRIQC on {len(subjects)} subject(s), one at a time.")
        self.progress.start(len(subjects), None, "Starting MRIQC...")
        self.worker.start()
        self.update_controls()

    # A subject's MRIQC run takes about the same number of steps per scan each time, so the
    # bar's estimate is the last complete run's steps per scan x this subject's scans.

    def subject_scans(self, subject: str) -> int:
        bids_root = self.state.folder("bids")
        return len(mq.bids_scans(bids_root, subject, self.selection())) if bids_root else 0

    def estimate_for(self, subject: str) -> int | None:
        per_scan = self.saved_number("mriqc_steps_per_scan", 0)
        return round(per_scan * self.subject_scans(subject)) or None

    def on_subject_started(self, subject: str) -> None:
        self.progress.estimate = self.estimate_for(subject)
        self.progress.subject_started(subject)
        self.set_subject_status(subject, RUNNING, "running")

    def on_step(self, subject: str, steps: int, node: str) -> None:
        self.progress.step(subject, steps, node)
        self.set_subject_status(subject, RUNNING, f"running ({self.progress.step_count(steps)})")

    def on_subject_done(self, subject: str, failure: str) -> None:
        state = self.progress.running.get(subject)
        scans = self.subject_scans(subject)
        if not failure and state is not None and state.steps and scans:
            self.state.settings.setValue("mriqc_steps_per_scan", state.steps / scans)
        self.progress.subject_finished(subject)
        bids_root = self.state.folder("bids")
        status = mq.DONE
        if bids_root is not None:
            status = mq.subject_state(
                bids_root, self.state.folder("mriqc"), subject, self.selection()
            )
        self.statuses[subject] = status
        self.set_subject_status(subject, FAILED if failure else status)
        self.load_results()

    def set_subject_status(self, subject: str, status: str, note: str = "") -> None:
        if subject in self.subjects:
            row = self.subjects.index(subject)
            self.subject_table.setItem(row, 3, self.status_item(status, note))

    def on_all_done(self, summary: str) -> None:
        if self.worker is not None:
            self.worker.wait()
        self.worker = None
        self.progress.finish()
        self.state.set_busy(QUALITY_CHECKING, False)
        self.log(summary)
        self.refresh()
        self.update_controls()

    def on_stop(self) -> None:
        if self.worker is not None:
            self.log("Stopping MRIQC...")
            self.progress.set_stage("Stopping...")
            self.worker.stop()

    def log(self, text: str, error: bool = False) -> None:
        self.activity.appendPlainText(f"ERROR  {text}" if error else text)

    def close_check(self) -> bool:
        """Closing the window: True when it may close (MRIQC stopped, if it was running)."""
        if self.worker is None:
            self.docker_timer.stop()
            if self.probe is not None:  # a download isn't waited for
                self.probe.wait()
            self.docker_status.wait()
            return True
        answer = QMessageBox.question(
            self,
            "MRIQC is still running",
            "Stop MRIQC and close? The subject it is on is lost and runs again next time.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.worker.stop_requested = True
        if self.worker.current is not None:
            mq.stop_container(self.worker.docker, mq.container_name(self.worker.current))
        return True

    # ---- results -------------------------------------------------------------------------------

    def on_subject_selected(self) -> None:
        """Selecting a subject on the left jumps to it in the results."""
        rows = {index.row() for index in self.subject_table.selectedIndexes()}
        if len(rows) != 1:
            return
        name = f"sub-{self.subjects[rows.pop()]}"
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item is not None and item.text(0) == name:
                self.tree.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)
                return

    # ---- the selected run: its view and rating -------------------------------------------------

    def selected_run(self) -> str:
        """The selected run's name, '' when a subject or session row (or nothing) is selected."""
        items = self.tree.selectedItems()
        return str(items[0].data(0, Qt.ItemDataRole.UserRole) or "") if items else ""

    def on_run_selected(self) -> None:
        run = self.runs_by_name.get(self.selected_run())
        self.show_rating(run)
        if run is None:
            self.show_note("Select a run above to see the scan here and rate it.")
            return
        bids_root = self.state.folder("bids")
        path = bids_root / run.nifti if bids_root is not None and run.nifti is not None else None
        try:
            mtime = path.stat().st_mtime if path is not None else None
        except OSError:
            mtime = None
        if path is None or mtime is None:
            self.show_note(
                f"{run.name}\n\nThe BIDS scan isn't here, so it can't be shown: rate it from "
                "the metrics above."
            )
            return
        self.request_view((str(path), mtime), path, run.modality)

    def show_note(self, text: str) -> None:
        self.shown_key = None
        self.scan_view.set_pixmap(QPixmap(), text)

    def request_view(self, key: tuple, path: Path, modality: str) -> None:
        """Show the scan's view: from the cache at once, else drawn in the background."""
        self.shown_key = key
        cached = self.views.get(key)
        if cached is not None:
            self.views.move_to_end(key)
            self.show_view(cached)
            return
        self.scan_view.set_pixmap(QPixmap(), f"Drawing {path.name}...")
        self.wanted = (key, path, modality)
        self.start_next_view()

    def start_next_view(self) -> None:
        if self.renderer is not None or self.wanted is None:
            return  # one at a time: the wanted one starts when this finishes
        key, path, modality = self.wanted
        self.wanted = None
        self.renderer = RenderThread(key, path, modality)
        self.renderer.done.connect(self.on_view_drawn)
        self.renderer.start()

    def on_view_drawn(self, key: tuple, image: QImage | None, error: str) -> None:
        if self.renderer is not None:
            self.renderer.wait()
        self.renderer = None
        view: QPixmap | str = QPixmap.fromImage(image) if image is not None else error
        self.views[key] = view
        while len(self.views) > VIEW_CACHE_SIZE:
            self.views.popitem(last=False)
        if key == self.shown_key:
            self.show_view(view)
        self.start_next_view()

    def show_view(self, view: QPixmap | str) -> None:
        if isinstance(view, str):
            self.scan_view.set_pixmap(QPixmap(), view)
        else:
            self.scan_view.set_pixmap(view)

    def set_rating_enabled(self, enabled: bool) -> None:
        for button in self.rating_group.buttons():
            button.setEnabled(enabled)
        self.comment.setEnabled(enabled)

    def show_rating(self, run: mq.RunIqms | None) -> None:
        """The selected run's saved rating and comment in the rating row."""
        output_root = self.state.folder("mriqc")
        self.set_rating_enabled(run is not None and output_root is not None)
        rating = self.ratings.get(run.name) if run is not None else None
        self.rating_group.setExclusive(False)  # so every button can be unchecked
        for button in self.rating_group.buttons():
            button.setChecked(rating is not None and self.rating_group.id(button) == rating.rating)
        self.rating_group.setExclusive(True)
        self.comment.blockSignals(True)
        self.comment.setText(rating.comment if rating else "")
        self.comment.blockSignals(False)
        self.rating_title.setText(f"QC rating of {run.label}:" if run else "QC rating:")
        self.rating_note.setText(f"by {rating.rater}" if rating and rating.rater else "")

    def on_rating_clicked(self, _value: int) -> None:
        self.save_current_rating()

    def save_current_rating(self) -> None:
        run, output_root = self.selected_run(), self.state.folder("mriqc")
        if not run or output_root is None:
            return
        value = self.rating_group.checkedId()
        rating = value if value in mq.RATINGS else None
        comment = self.comment.text().strip()
        old = self.ratings.get(run)
        if old is not None and (old.rating, old.comment) == (rating, comment):
            return
        if old is None and rating is None and not comment:
            return
        try:
            saved = mq.save_rating(output_root, run, rating, comment)
        except OSError as e:
            QMessageBox.warning(self, "Rating not saved", f"Couldn't write the ratings file: {e}")
            return
        if saved is None:
            self.ratings.pop(run, None)
        else:
            self.ratings[run] = saved
        items = self.tree.selectedItems()
        if items:
            self.show_rating_in(items[0], saved)
        self.rating_note.setText("saved")

    def stop_drawing(self) -> None:
        """Closing: wait for a view being drawn (a running QThread must not be destroyed)."""
        self.wanted = None
        if self.renderer is not None:
            self.renderer.wait()

    def on_show_formulas(self) -> None:
        """The formulas of the scans shown (BOLD or T1w)."""
        from fmri_local_process.gui.metric_help_dialog import MetricHelpDialog
        from fmri_local_process.processing.mriqc_docs import MRIQC_DOCS

        modality = str(self.modality_box.currentData())
        dialog = self.formulas_dialogs.get(modality)
        if dialog is None:  # rendering the equations takes a moment: once per modality
            dialog = MetricHelpDialog(
                self,
                MRIQC_DOCS[modality],
                f"How MRIQC's {MODALITY_TEXT[modality]} metrics are calculated",
                f"MRIQC metric formulas: {MODALITY_TEXT[modality]}",
            )
            self.formulas_dialogs[modality] = dialog
        dialog.show()
        dialog.raise_()
