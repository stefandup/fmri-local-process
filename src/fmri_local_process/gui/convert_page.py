"""The Setup & Convert to BIDS tab: the raw folders, the BIDS folder they are converted into,
the study's settings, and the DICOM -> BIDS converter with its progress and last report.

Re-running is safe: existing files are kept, deleted ones are recreated from the raw data,
and corrections saved on the other tabs (IDs, logs, deleted sessions) are applied.
"""

import logging
from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.gui.backup_box import BackupBox
from fmri_local_process.gui.docker_controls import DockerControlThread
from fmri_local_process.gui.folder_panel import FolderPanel
from fmri_local_process.gui.image_view import HelpToggle
from fmri_local_process.gui.qt_common import (
    ERROR_COLOR,
    HOME_BASE_ACCENT_COLOR,
    PROCESS_ACCENT_COLOR,
    PROCESS_ACCENT_HOVER_COLOR,
    RAW_ACCENT_COLOR,
    WARNING_COLOR,
    NoticeBanner,
    accent_group_box_stylesheet,
    primary_action_stylesheet,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.gui.scan_rules_dialog import ScanRulesDialog
from fmri_local_process.gui.toolbox_state import CONVERTING, ToolboxState
from fmri_local_process.processing.crosscheck import (
    read_expected_sessions,
    replay_decisions,
    write_expected_sessions,
)
from fmri_local_process.processing.crosscheck_backup import restore_backup
from fmri_local_process.processing.dcm2niix import (
    Dcm2niixNotFoundError,
    find_dcm2niix,
    run_dcm2niix,
)
from fmri_local_process.processing.deface import (
    DefaceUnavailableError,
    DockerNeededError,
    make_deface,
)
from fmri_local_process.processing.philani_bids import ConversionReport, convert_dicom_root

PACKAGE_LOGGER = "fmri_local_process"
MAX_SESSIONS = 20


class LogEmitter(QObject):
    line = Signal(str)


class QtLogHandler(logging.Handler):
    """Relays the converter's log records to the activity pane (thread-safe via a signal)."""

    def __init__(self, emitter: LogEmitter) -> None:
        super().__init__(logging.INFO)
        self.emitter = emitter
        self.setFormatter(logging.Formatter("%(levelname)s  %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.emitter.line.emit(self.format(record))


class DefacerProbe(QThread):
    """Looks for a defacer (make_deface finds one or says why not) off the GUI thread."""

    done = Signal(str, str)  # why not ("" when one was found), DOCKER_* when Docker is why

    def run(self) -> None:
        try:
            make_deface()
        except DockerNeededError as e:
            self.done.emit(str(e), DOCKER_STOPPED if e.installed else DOCKER_MISSING)
            return
        except (DefaceUnavailableError, OSError) as e:
            self.done.emit(str(e), "")
            return
        self.done.emit("", "")


DOCKER_STOPPED, DOCKER_MISSING = "stopped", "missing"
DEFACER_WARNINGS = {
    DOCKER_STOPPED: "⚠ Docker isn't running: can't check for the defacer",
    DOCKER_MISSING: "⚠ Docker not installed: T1w won't be defaced",
    "": "⚠ No defacer: T1w won't be defaced",
}


class ConvertThread(QThread):
    done = Signal(object)  # ConversionReport, or an error message
    progress = Signal(int, int, str)  # steps done, total steps, what is starting now

    def __init__(self, dicom_root: Path, bids_root: Path, behav_root: Path | None, deface: bool):
        super().__init__()
        self.dicom_root, self.bids_root, self.behav_root = dicom_root, bids_root, behav_root
        self.deface = deface

    def run(self) -> None:
        try:
            exe = find_dcm2niix()
            deface = make_deface() if self.deface else None
            report = convert_dicom_root(
                self.dicom_root,
                self.bids_root,
                partial(run_dcm2niix, exe),
                deface,
                self.behav_root,
                progress=self.progress.emit,  # queued to the GUI thread
            )
        except (Dcm2niixNotFoundError, DefaceUnavailableError, OSError, ValueError) as e:
            self.done.emit(str(e))
            return
        self.done.emit(report)


def report_rows(report: ConversionReport) -> list[tuple[str, int, str]]:
    """(what, how many, colour) for the summary table, as the CLI prints it."""
    return [
        ("converted / updated", len(report.converted), ""),
        ("repeated runs to pick at Crosscheck", len(report.to_pick), WARNING_COLOR),
        ("logs not placed in a session", len(report.unplaced), WARNING_COLOR),
        ("sessions renumbered", len(report.renumbered), WARNING_COLOR),
        ("converted from incomplete data", len(report.flagged), WARNING_COLOR),
        ("missing behavioural logs", len(report.missing), WARNING_COLOR),
        ("skipped sessions (>1 GB, done)", len(report.skipped), ""),
        ("sessions left out (deleted at Crosscheck)", len(report.excluded), ""),
        ("sessions with no recognised scans", len(report.no_scans), WARNING_COLOR),
        ("failed", len(report.failures), ERROR_COLOR),
    ]


def report_lines(report: ConversionReport) -> list[str]:
    lines = [f"INCOMPLETE  {what}: {why}" for what, why in report.flagged]
    lines += [f"RENUMBERED  {change}" for change in report.renumbered]
    lines += [f"LEFT OUT  {folder} (deleted at Crosscheck)" for folder in report.excluded]
    lines += [
        f"NO SCANS  {folder}: no series folder matches a scan rule (found: {found})"
        for folder, found in report.no_scans
    ]
    lines += [f"NOT PLACED  {key}: {why} -- see Behaviour logs" for key, why in report.unplaced]
    lines += [f"MISSING  {what} log" for what in report.missing]
    lines += [f"FAILED  {what}: {why}" for what, why in report.failures]
    return lines


def has_subfolders(folder: Path) -> bool:
    try:
        return any(child.is_dir() for child in folder.iterdir())
    except OSError:  # gone, or (OneDrive) not synced
        return False


def setup_notices(state: ToolboxState) -> list[str]:
    """What the Setup tab is missing: folders not chosen, or a DICOM folder with no sessions."""
    dicom, behav, bids = (state.folder(key) for key in ("dicom", "behav", "bids"))
    messages = []
    if dicom is None:
        messages.append("No DICOM sessions folder chosen: Browse for it above.")
    elif not has_subfolders(dicom):
        messages.append(f"No session folders in {dicom}: nothing to convert.")
    if behav is None:
        messages.append(
            "No behavioural logs folder chosen: the BOLD runs get no events files "
            "(Browse for it above if this study has logs)."
        )
    if bids is None:
        messages.append("No BIDS folder chosen: Browse for the folder to convert into.")
    return messages


HELP_TEXT = (
    "<b>Setup & Convert to BIDS</b><br>"
    "Choose the raw folders on the left and the BIDS folder they are converted into on the "
    "right; every tab works on these folders, and they are remembered.<br><br>"
    "<b>DICOM sessions (raw)</b>: one folder per scan session, named "
    "&lt;YYYYMMDD&gt;_&lt;time&gt;_&lt;subject ID&gt;. <b>Behavioural logs (raw)</b> "
    "(optional): the top folder of the logs; every .log below it is found, its task from "
    "'cogreappraisal' / 'emolabeling' in its path. Raw folders are only read, never changed."
    "<br><br>"
    "<b>Convert to BIDS</b> puts every session into its ses-&lt;n&gt; in date order, with the "
    "logs' events. Re-running is safe: existing files are kept, deleted ones are recreated "
    "from the raw data, and the corrections saved on the other tabs (IDs, logs, deleted "
    "sessions) are applied. Each scan is converted in a temporary folder and only moved into "
    "BIDS once it (and its defacing) worked.<br><br>"
    "<b>Scan rules...</b>: which DICOM series folders become which BIDS scan (the Philani "
    "protocol names, plus your own, e.g. a clinical scan's t1_mprage as the T1w).<br><br>"
    "<b>Sessions per subject</b>: how many visits this study has; a subject with more is "
    "flagged ⊕ on the BIDS Crosscheck tab, so a dud visit stands out."
)
ARROW_POINT_SIZE = 40


class ConvertPage(QWidget):
    """The Setup & Convert to BIDS tab: raw folders -> BIDS folder, the study's settings,
    and the converter."""

    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        self.convert_thread: ConvertThread | None = None
        self.emitter = LogEmitter()
        self.emitter.line.connect(self.log_line)
        self.log_handler = QtLogHandler(self.emitter)

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        raw = QVBoxLayout()
        raw.addWidget(
            FolderPanel(
                state,
                "dicom",
                "DICOM sessions (raw)",
                "The raw scans: one folder per scan session, named "
                "<YYYYMMDD>_<time>_<subject ID>. Only read, never changed.",
                border_color=RAW_ACCENT_COLOR,
            )
        )
        raw.addWidget(
            FolderPanel(
                state,
                "behav",
                "Behavioural logs (raw)",
                "Optional: the top folder of the behavioural logs. Every .log below it is "
                "found; its task comes from 'cogreappraisal' / 'emolabeling' in its path. "
                "Only read, never changed.",
                border_color=RAW_ACCENT_COLOR,
            )
        )
        top.addLayout(raw, 1)
        arrow = QLabel("➜")
        arrow_font = arrow.font()
        arrow_font.setPointSize(ARROW_POINT_SIZE)
        arrow.setFont(arrow_font)
        arrow.setStyleSheet(f"color: {HOME_BASE_ACCENT_COLOR};")
        arrow.setToolTip("Converted into")
        top.addWidget(arrow, 0, Qt.AlignmentFlag.AlignVCenter)

        bids_column = QVBoxLayout()
        bids_column.addWidget(
            FolderPanel(
                state,
                "bids",
                "BIDS Folder",
                "Where Convert to BIDS writes. The Scan sessions, Behaviour logs, Scan lengths, "
                "BIDS Crosscheck and Preprocess tabs all work on it.",
                home_base=True,
            )
        )
        options = QGroupBox("Study and conversion")
        options.setStyleSheet(accent_group_box_stylesheet(HOME_BASE_ACCENT_COLOR))
        options_layout = QHBoxLayout(options)
        sessions_label = QLabel("Sessions per subject:")
        self.sessions_spin = QSpinBox()
        self.sessions_spin.setRange(1, MAX_SESSIONS)
        sessions_tip = wrap_tooltip(
            "How many sessions each subject should have in this study. A subject with more "
            "is flagged ⊕ on the BIDS Crosscheck tab: one may be a dud visit to delete. "
            "Saved in the BIDS folder (code/crosscheck_settings.json)."
        )
        sessions_label.setToolTip(sessions_tip)
        self.sessions_spin.setToolTip(sessions_tip)
        self.sessions_spin.valueChanged.connect(self.on_expected_sessions)
        options_layout.addWidget(sessions_label)
        options_layout.addWidget(self.sessions_spin)
        options_layout.addSpacing(16)
        self.deface_box = QCheckBox("Deface T1w")
        self.deface_box.setToolTip(
            wrap_tooltip(
                "Uses mri_deface from $FREESURFER_HOME, else the defacer the installer set up "
                "in Docker (FreeSurfer mri_deface or pydeface)."
            )
        )
        self.deface_box.setChecked(str(state.settings.value("deface", "true")).lower() == "true")
        self.deface_box.toggled.connect(lambda on: state.settings.setValue("deface", on))
        self.deface_box.setEnabled(False)  # until the defacer check below has run
        options_layout.addWidget(self.deface_box)
        self.deface_warning = QLabel()
        self.deface_warning.setStyleSheet(f"color: {WARNING_COLOR}; font-weight: bold;")
        self.deface_warning.setVisible(False)
        options_layout.addWidget(self.deface_warning)
        self.start_docker_button = QPushButton("Start Docker")
        self.start_docker_button.setToolTip(
            wrap_tooltip("Start Docker Desktop (can take a minute), then check for the defacer.")
        )
        self.start_docker_button.setVisible(False)
        self.start_docker_button.clicked.connect(self.start_docker)
        options_layout.addWidget(self.start_docker_button)
        self.recheck_button = QPushButton("Check again")
        self.recheck_button.setToolTip(
            wrap_tooltip("Look for the defacer again, e.g. after starting Docker yourself.")
        )
        self.recheck_button.setVisible(False)
        self.recheck_button.clicked.connect(self.check_defacer)
        options_layout.addWidget(self.recheck_button)
        self.docker_thread: DockerControlThread | None = None
        options_layout.addStretch(1)
        self.help = HelpToggle("Help ?", "What this tab does, step by step.")
        self.help.set_text(HELP_TEXT)
        self.rules_button = QPushButton("Scan rules...")
        self.rules_button.setToolTip(
            wrap_tooltip(
                "Which DICOM series folders become which BIDS scan: the Philani protocol "
                "names, plus your own (e.g. a clinical scan's T1). Saved with the BIDS folder."
            )
        )
        self.rules_button.clicked.connect(self.on_scan_rules)
        options_layout.addWidget(self.rules_button)
        options_layout.addWidget(self.help.button)
        self.convert_button = QPushButton("Convert to BIDS")
        self.convert_button.setToolTip(
            wrap_tooltip(
                "Convert every DICOM session (and behavioural log) into the BIDS folder. "
                "Existing files are kept; deleted ones are recreated from the raw data."
            )
        )
        self.convert_button.setStyleSheet(
            primary_action_stylesheet(PROCESS_ACCENT_COLOR, PROCESS_ACCENT_HOVER_COLOR)
        )
        self.convert_button.clicked.connect(self.on_convert)
        options_layout.addWidget(self.convert_button)
        bids_column.addWidget(options)
        self.backup = BackupBox(state)
        self.backup.message.connect(self.log_line)
        self.backup.restore_requested.connect(self.on_restore)
        bids_column.addWidget(self.backup)
        top.addLayout(bids_column, 1)
        self.replay_after_conversion = False  # set by a restore
        self.defacer_probe: DefacerProbe | None = None
        self.defacer_error = ""  # why defacing isn't available ("" when it is)
        layout.addLayout(top)
        self.banner = NoticeBanner()
        layout.addWidget(self.banner)
        layout.addWidget(self.help.box)

        self.lock_label = QLabel()
        self.lock_label.setStyleSheet(f"color: {WARNING_COLOR};")
        layout.addWidget(self.lock_label)
        layout.addWidget(self._build_progress_group())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        summary = QGroupBox("Last conversion")
        summary_layout = QVBoxLayout(summary)
        self.summary_table = QTableWidget(0, 2)
        self.summary_table.setHorizontalHeaderLabels(["Result", "Count"])
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.summary_table.horizontalHeader().setStretchLastSection(True)
        summary_layout.addWidget(self.summary_table)
        splitter.addWidget(summary)
        activity = QGroupBox("Activity Log")
        activity_layout = QVBoxLayout(activity)
        self.activity = QTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setPlaceholderText("What the converter does appears here.")
        activity_layout.addWidget(self.activity)
        splitter.addWidget(activity)
        splitter.setSizes([380, 920])
        layout.addWidget(splitter, 1)

        state.folder_changed.connect(lambda _key: self.update_controls())
        state.folder_changed.connect(lambda _key: self.update_notices())
        state.busy_changed.connect(self.update_controls)
        self.update_controls()
        self.update_notices()
        self.check_defacer()

    def _build_progress_group(self) -> QGroupBox:
        """Large progress panel (as in the Preprocess tab), shown only while converting."""
        group = QGroupBox("Converting")
        group.setStyleSheet(accent_group_box_stylesheet(PROCESS_ACCENT_COLOR))
        layout = QVBoxLayout(group)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumHeight(30)
        bar_font = self.progress_bar.font()
        bar_font.setPointSize(bar_font.pointSize() + 3)
        bar_font.setBold(True)
        self.progress_bar.setFont(bar_font)
        self.progress_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {PROCESS_ACCENT_COLOR}; }}"
            "QProgressBar { text-align: center; }"
        )
        layout.addWidget(self.progress_bar)
        self.progress_label = QLabel()
        style_secondary_label(self.progress_label)
        layout.addWidget(self.progress_label)
        group.setVisible(False)
        self.progress_group = group
        return group

    def show_page(self) -> None:
        self.update_controls()
        self.update_notices()

    def update_notices(self) -> None:
        self.banner.set_messages(setup_notices(self.state))

    def update_controls(self) -> None:
        dicom, bids = self.state.folder("dicom"), self.state.folder("bids")
        self.sessions_spin.blockSignals(True)
        self.sessions_spin.setValue(read_expected_sessions(bids) if bids else 1)
        self.sessions_spin.setEnabled(bids is not None)
        self.sessions_spin.blockSignals(False)
        reason = self.state.lock_reason(CONVERTING)
        self.lock_label.setText(f"Locked: {reason}." if reason else "")
        self.lock_label.setVisible(bool(reason))
        ready = (
            dicom is not None and bids is not None and self.convert_thread is None and not reason
        )
        self.convert_button.setEnabled(ready)
        self.deface_box.setEnabled(
            self.convert_thread is None and self.defacer_probe is None and not self.defacer_error
        )

    def on_scan_rules(self) -> None:
        bids_root = self.state.folder("bids")
        if bids_root is None:
            QMessageBox.information(
                self,
                "BIDS folder needed",
                "Choose the BIDS folder first: the rules are kept in it.",
            )
            return
        if ScanRulesDialog(bids_root, self.state.folder("dicom"), self).exec():
            self.log_line("Scan rules saved: Convert to BIDS to apply them.")
            self.state.bump()

    # ---- defacer ----------------------------------------------------------------------------

    def check_defacer(self) -> None:
        """Look for a defacer in the background (it may ask Docker, which takes a moment)."""
        if self.defacer_probe is not None:
            return
        self.deface_warning.setText("Checking for a defacer...")
        self.deface_warning.setVisible(True)
        self.recheck_button.setVisible(False)
        self.start_docker_button.setVisible(False)
        self.defacer_probe = DefacerProbe()
        self.defacer_probe.done.connect(self.on_defacer_checked)
        self.defacer_probe.start()
        self.update_controls()

    def start_docker(self) -> None:
        if self.docker_thread is not None:
            return
        self.deface_warning.setText("Starting Docker...")
        self.start_docker_button.setEnabled(False)
        self.recheck_button.setEnabled(False)
        self.docker_thread = DockerControlThread("start")
        self.docker_thread.done.connect(self.on_docker_started)
        self.docker_thread.start()

    def on_docker_started(self, _action: str, error: str) -> None:
        if self.docker_thread is not None:
            self.docker_thread.wait()
        self.docker_thread = None
        self.start_docker_button.setEnabled(True)
        self.recheck_button.setEnabled(True)
        if error:
            self.log_line(f"Couldn't start Docker: {error}")
        self.check_defacer()

    def on_defacer_checked(self, error: str, docker: str = "") -> None:
        if self.defacer_probe is not None:
            self.defacer_probe.wait()
        self.defacer_probe = None
        self.defacer_error = error
        self.deface_warning.setVisible(bool(error))
        self.recheck_button.setVisible(bool(error))
        self.start_docker_button.setVisible(bool(error) and docker == DOCKER_STOPPED)
        self.deface_warning.setText(DEFACER_WARNINGS[docker])
        self.deface_warning.setToolTip(wrap_tooltip(error))
        self.deface_box.setToolTip(
            wrap_tooltip(
                "Uses mri_deface from $FREESURFER_HOME, else the defacer the installer set up "
                "in Docker (FreeSurfer mri_deface or pydeface)."
                + (f"\n\nNot available: {error}" if error else "")
            )
        )
        self.update_controls()

    def deface_wanted(self) -> bool:
        """Deface only when it is ticked and a defacer was found (greyed out otherwise)."""
        return self.deface_box.isChecked() and not self.defacer_error

    # ---- restore ---------------------------------------------------------------------------

    def on_restore(self, backup: Path) -> None:
        bids_root = self.state.folder("bids")
        if bids_root is None or self.state.folder("dicom") is None:
            QMessageBox.information(
                self, "Folders needed", "Choose the DICOM sessions and BIDS folders first."
            )
            return
        if reason := self.state.lock_reason():
            QMessageBox.information(self, "Not now", f"Can't restore now: {reason}.")
            return
        has_sessions = any(bids_root.glob("sub-*/ses-*"))
        warning = (
            "\n\nThis BIDS folder already has converted sessions. Restore is meant for an "
            "empty or freshly made BIDS folder: its own corrections, notes and settings are "
            "replaced by the saved ones."
            if has_sessions
            else ""
        )
        answer = QMessageBox.question(
            self,
            "Restore crosscheck",
            f"Restore the crosscheck saved in {backup}?\n\nIts corrections, deleted sessions, "
            "notes and settings go back into the BIDS folder, everything is converted again "
            "from raw, and then the kept runs and trims are replayed." + warning,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            restored = restore_backup(backup, bids_root)
        except OSError as e:
            QMessageBox.warning(self, "Not restored", str(e))
            return
        self.log_line(f"Restored {len(restored)} crosscheck file(s) from {backup}; converting...")
        self.state.bump()
        self.replay_after_conversion = True
        self.on_convert()

    def replay(self) -> None:
        bids_root = self.state.folder("bids")
        if bids_root is None:
            return
        applied, unresolved = replay_decisions(bids_root)
        for line in applied:
            self.log_line(f"REPLAYED  {line}")
        for line in unresolved:
            self.log_line(f"NOT REPLAYED  {line}")
        text = f"Restore finished: {len(applied)} kept run(s) / trim(s) replayed."
        if unresolved:
            text += f"\n{len(unresolved)} could not be (see the Activity Log)."
            QMessageBox.warning(self, "Restore finished", text)
        else:
            QMessageBox.information(self, "Restore finished", text)

    def on_expected_sessions(self, sessions: int) -> None:
        bids_root = self.state.folder("bids")
        if bids_root is None:
            return
        try:
            write_expected_sessions(bids_root, sessions)
        except OSError as e:
            QMessageBox.warning(self, "Not saved", str(e))
            return
        self.state.bump()

    def on_convert_progress(self, done: int, total: int, what: str) -> None:
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)
        self.progress_bar.setFormat("%v / %m steps (%p%)")
        self.progress_label.setText(f"Now: {what}" if done < total else "Finishing up...")

    def on_convert(self) -> None:
        bids_root, dicom_root = self.state.folder("bids"), self.state.folder("dicom")
        if bids_root is None or dicom_root is None or self.convert_thread is not None:
            return
        self.log_line(f"Converting {dicom_root} -> {bids_root} ...")
        logging.getLogger(PACKAGE_LOGGER).addHandler(self.log_handler)
        self.convert_thread = ConvertThread(
            dicom_root, bids_root, self.state.folder("behav"), self.deface_wanted()
        )
        self.convert_thread.done.connect(self.on_convert_done)
        self.convert_thread.finished.connect(self.on_convert_finished)
        self.convert_thread.progress.connect(self.on_convert_progress)
        self.progress_bar.setRange(0, 0)  # busy until the first step: sessions are being found
        self.progress_label.setText("Finding sessions and behavioural logs...")
        self.progress_group.setVisible(True)
        self.state.set_busy(CONVERTING, True)
        self.update_controls()
        self.convert_thread.start()

    def on_convert_finished(self) -> None:
        """The thread ended without reporting (an unexpected error): don't leave the tab
        stuck. `done` always arrives first when the run completes."""
        if self.convert_thread is not None:
            self.on_convert_done(
                "The converter stopped unexpectedly. The error is in the console window; "
                "please pass it on."
            )

    def on_convert_done(self, result: ConversionReport | str) -> None:
        logging.getLogger(PACKAGE_LOGGER).removeHandler(self.log_handler)
        if self.convert_thread is not None:
            self.convert_thread.wait()
        self.convert_thread = None
        self.progress_group.setVisible(False)
        self.state.set_busy(CONVERTING, False)
        self.update_controls()
        replay, self.replay_after_conversion = self.replay_after_conversion, False
        if isinstance(result, str):
            QMessageBox.warning(self, "Conversion failed", result)
            self.log_line(f"ERROR  {result}")
        else:
            self.show_report(result)
            if replay:
                self.replay()
        self.state.bump()

    def show_report(self, report: ConversionReport) -> None:
        rows = report_rows(report)
        self.summary_table.setRowCount(len(rows))
        for row, (what, count, color) in enumerate(rows):
            for column, text in enumerate((what, str(count))):
                item = QTableWidgetItem(text)
                if color and count:
                    item.setForeground(QColor(color))
                self.summary_table.setItem(row, column, item)
        self.summary_table.resizeColumnsToContents()
        self.log_line("Done.")
        for line in report_lines(report):
            self.log_line(line)

    def log_line(self, text: str) -> None:
        self.activity.append(text)
