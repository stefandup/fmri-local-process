"""Raw DICOM sessions and behaviour logs: match each log to a scan session, look at its events,
record a verdict. Two toolbox tabs, Raw DICOMs and Behaviour logs, share one panel.

* A wrong ID or date in a log's name is corrected here; so is a wrong subject ID in a DICOM
  session folder's name (Raw DICOMs tab: each folder with its scan date/time and the ID
  as written, e.g. a twin's 12345-1, which becomes 12345T1).
* Selecting a log shows the events it produces (and whether they fit the converted BOLD run),
  so it can be marked OK or as an error. Once looked at, logs are marked crosschecked (several
  selected rows at once).
* Of several logs on one day, "Use this one" picks the log that goes into BIDS.

Everything is saved in the BIDS folder (code/behav_corrections.json and
code/dicom_id_corrections.json), never by renaming or editing the raw files (vr_lab_toolbox's
rule). Convert / update BIDS applies it.
"""

import datetime as dt
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIntValidator, QValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.app.behav import (
    BAD_COLOR,
    COLUMNS,
    CORRECTED_DATE,
    CORRECTED_ID,
    EXPORT_INTO_BIDS_QUESTION,
    EXPORT_LAYOUTS,
    EXPORT_SUBFOLDER,
    FIX_RED,
    HELP_TEXT,
    INTO_BIDS,
    INTO_OUTPUT,
    ISO_PATTERN,
    MATCH,
    MAX_SESSION,
    S_CHECKED,
    S_CORRECTED,
    S_DATE,
    S_FIXED,
    S_NOTES,
    S_TASKS,
    SESSION_COLUMNS,
    SESSIONS_HELP_TEXT,
    STATE,
    VERDICT,
    VERDICT_COLORS,
    WARN_COLOR,
    BehavData,
    BehavWork,
    behav_folders,
    event_rows,
    export_text,
    folder_name_parts,
    inventory_text,
    load_behav_data,
    log_cells,
    log_title,
    missing_folders_notice,
    parse_date,
)
from fmri_local_process.app.colors import OK_COLOR
from fmri_local_process.app.crosscheck import CROSSCHECKED_COLOR, CROSSCHECKED_ICON
from fmri_local_process.app.state import VIEW_ONLY
from fmri_local_process.gui.crosscheck_gui import mark_button
from fmri_local_process.gui.image_view import HelpToggle
from fmri_local_process.gui.qt_common import NoticeBanner
from fmri_local_process.gui.toolbox_state import ToolboxState
from fmri_local_process.processing.behav_export import (
    BIDS_LAYOUT,
)
from fmri_local_process.processing.behav_logs import (
    canonical_id,
)
from fmri_local_process.processing.behav_match import (
    COMPLETE,
    CORRECTIONS_FILE,
    ISO_DATE,
    MATCHED,
    REVIEW_ERROR,
    REVIEW_OK,
    LogInfo,
    LogMatch,
    events_summary,
    needs_review,
    same_day_rivals,
)
from fmri_local_process.processing.philani_bids import (
    DICOM_CORRECTIONS_FILE,
    ScannedFolder,
)


class DateValidator(QValidator):
    """Only a real YYYY-MM-DD date can be typed (empty is fine: no correction)."""

    def validate(self, text: str, pos: int) -> tuple[QValidator.State, str, int]:
        if not text:
            return QValidator.State.Acceptable, text, pos
        if not ISO_PATTERN.fullmatch(text):
            return QValidator.State.Invalid, text, pos
        if len(text) < len("YYYY-MM-DD"):
            return QValidator.State.Intermediate, text, pos
        try:
            dt.date.fromisoformat(text)
        except ValueError:  # e.g. 2024-02-30
            return QValidator.State.Invalid, text, pos
        return QValidator.State.Acceptable, text, pos


def session_validator(parent: QWidget) -> QIntValidator:
    """Only a session number (1, 2, ...) can be typed."""
    return QIntValidator(1, MAX_SESSION, parent)


class BehavLoadThread(QThread):
    progress = Signal(int, int, str)
    done = Signal(object)  # BehavData, or an error message

    def __init__(self, bids_root: Path, behav_root: Path, dicom_root: Path) -> None:
        super().__init__()
        self.roots = (bids_root, behav_root, dicom_root)

    def run(self) -> None:
        try:
            data = load_behav_data(*self.roots, progress=self.progress.emit)
        except (OSError, ValueError) as e:
            self.done.emit(str(e))
            return
        self.done.emit(data)


ROWS_PER_PROGRESS_STEP = 25


class BehavLogsPanel(QWidget):
    saved = Signal()

    def __init__(
        self,
        bids_root: Path,
        behav_root: Path,
        dicom_root: Path,
        parent: QWidget | None,
        data: BehavData | None = None,
        output_root: Path | None = None,
    ):
        super().__init__(parent)
        self.bids_root = bids_root
        if data is None:  # read here and now (the tab reads it on a worker thread instead)
            QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                data = load_behav_data(bids_root, behav_root, dicom_root)
            finally:
                QGuiApplication.restoreOverrideCursor()
        self.work = BehavWork(bids_root, behav_root, dicom_root, data)
        self.work.output_root = output_root  # C-PAC's output folder (Preprocess tab), if chosen

        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.problems_only = QCheckBox("Needs review only")
        self.problems_only.setToolTip("Hide logs that are fine or already have a verdict.")
        self.problems_only.toggled.connect(self.apply_filter)
        controls.addWidget(self.problems_only)
        self.recheck_button = QPushButton("Re-check")
        self.recheck_button.setToolTip(
            "Match again with the corrections typed so far (not saved yet)."
        )
        self.recheck_button.clicked.connect(self.recheck)
        controls.addWidget(self.recheck_button)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("Re-checking %v / %m logs")
        self.progress_bar.setVisible(False)
        controls.addWidget(self.progress_bar, 1)
        controls.addStretch(1)
        self.summary = QLabel()
        controls.addWidget(self.summary)
        self.save_button = QPushButton("Save")
        self.save_button.setToolTip(
            "Keep the corrections and verdicts in the BIDS folder; Convert to BIDS applies them."
        )
        self.save_button.clicked.connect(self.save)
        controls.addWidget(self.save_button)
        self.export_button = QPushButton("Export logs...")
        self.export_button.setToolTip(
            "Save every log as an events.tsv: into the BIDS folder (tab 1), into C-PAC's "
            f"output folder ({EXPORT_SUBFOLDER}/), or a folder you choose (all in one folder, "
            "or in BIDS sub-/ses-/func folders), using the corrections and verdicts here. No "
            "scans are converted; logs marked error or not used are left out."
        )
        self.export_button.clicked.connect(self.on_export)
        controls.addWidget(self.export_button)
        self.help = HelpToggle("Help ?", "How to match, correct and judge the logs.")
        self.help.set_text(
            HELP_TEXT + f"<br><br>Saved in {bids_root / CORRECTIONS_FILE} and "
            f"{bids_root / DICOM_CORRECTIONS_FILE}."
        )
        controls.addWidget(self.help.button)
        layout.addLayout(controls)
        layout.addWidget(self.help.box)

        # The Raw DICOMs tab shows this (the toolbox puts it on a tab of its own); it shares
        # this panel's data, Re-check and Save.
        self.sessions_view = self._build_sessions_tab()
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_table())
        tabs = QTabWidget()
        tabs.addTab(self._build_detail(), "Selected log")
        self.contents = QTextEdit()
        self.contents.setReadOnly(True)
        self.contents.setPlainText(inventory_text(self.infos))
        tabs.addTab(self.contents, "Contents per task")
        splitter.addWidget(tabs)
        splitter.setSizes([450, 400])
        layout.addWidget(splitter, 1)
        self.show_matches()
        self.show_detail()

    @property
    def infos(self) -> list[LogInfo]:
        return self.work.infos

    @property
    def scanned(self) -> list[ScannedFolder]:
        return self.work.scanned

    @property
    def matches(self) -> list[LogMatch]:
        return self.work.matches

    # ---- layout -------------------------------------------------------------------------

    def _build_table(self) -> QTableWidget:
        self.table = QTableWidget(len(self.infos), len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self.show_detail)
        self.id_edits: list[QLineEdit] = []
        self.date_edits: list[QLineEdit] = []
        for row, info in enumerate(self.infos):
            id_edit = QLineEdit(info.correction.subject or "")
            id_edit.setPlaceholderText(info.file_subject or "ID")
            date_edit = QLineEdit(
                info.correction.date.strftime(ISO_DATE) if info.correction.date else ""
            )
            date_edit.setPlaceholderText("YYYY-MM-DD")
            self.table.setCellWidget(row, CORRECTED_ID, id_edit)
            self.table.setCellWidget(row, CORRECTED_DATE, date_edit)
            self.id_edits.append(id_edit)
            self.date_edits.append(date_edit)
        return self.table

    def _build_sessions_tab(self) -> QWidget:
        """The Raw DICOMs view: Re-check / Save, the session table, and its Mark / Un-mark
        crosschecked row underneath."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.sessions_recheck_button = QPushButton("Re-check")
        self.sessions_recheck_button.setToolTip(
            "Number the sessions and match the logs again with the corrections typed so far "
            "(not saved yet)."
        )
        self.sessions_recheck_button.clicked.connect(self.recheck)
        controls.addWidget(self.sessions_recheck_button)
        controls.addStretch(1)
        self.sessions_status = QLabel()
        controls.addWidget(self.sessions_status)
        self.sessions_save_button = QPushButton("Save")
        self.sessions_save_button.setToolTip(
            "Keep the corrections and crosscheck marks in the BIDS folder (the Behaviour logs "
            "tab's too); Convert to BIDS applies them."
        )
        self.sessions_save_button.clicked.connect(self.save)
        controls.addWidget(self.sessions_save_button)
        self.sessions_help = HelpToggle("Help ?", "Correcting and checking the scan sessions.")
        self.sessions_help.set_text(SESSIONS_HELP_TEXT)
        controls.addWidget(self.sessions_help.button)
        layout.addLayout(controls)
        layout.addWidget(self.sessions_help.box)
        layout.addWidget(self._build_session_table(), 1)
        row = QHBoxLayout()
        self.sessions_selected_label = QLabel()
        row.addWidget(self.sessions_selected_label, 1)
        self.unmark_sessions_button = QPushButton("Un-mark crosschecked")
        self.unmark_sessions_button.setToolTip("Take the reviewed mark off the selected folder(s).")
        self.unmark_sessions_button.clicked.connect(lambda: self.set_folders_checked(False))
        row.addWidget(self.unmark_sessions_button)
        self.mark_sessions_button = mark_button(
            "Mark crosschecked",
            "You have been through the selected DICOM folder(s): mark them as reviewed. Select "
            "several rows (Ctrl/Shift-click or drag) to mark them all at once.",
        )
        self.mark_sessions_button.clicked.connect(lambda: self.set_folders_checked(True))
        row.addWidget(self.mark_sessions_button)
        layout.addLayout(row)
        self.update_session_buttons()
        return panel

    def _build_session_table(self) -> QTableWidget:
        """Every DICOM session folder: when it was scanned, its ID as written, a correction."""
        self.session_table = QTableWidget(len(self.scanned), len(SESSION_COLUMNS))
        self.session_table.setHorizontalHeaderLabels(list(SESSION_COLUMNS))
        self.session_table.verticalHeader().setVisible(False)
        self.session_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.session_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.session_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.session_table.itemSelectionChanged.connect(self.update_session_buttons)
        self.session_edits: dict[str, QLineEdit] = {}
        self.folder_date_edits: dict[str, QLineEdit] = {}
        self.folder_session_edits: dict[str, QLineEdit] = {}
        for row, (folder, tasks) in enumerate(self.scanned):
            when, raw_id = folder_name_parts(folder.name)
            for column, text in enumerate((folder.name, when, raw_id)):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.session_table.setItem(row, column, item)
            self.session_table.setItem(row, S_TASKS, QTableWidgetItem(", ".join(sorted(tasks))))
            edit = QLineEdit(self.work.fixes.ids.get(folder.name, ""))
            edit.setPlaceholderText(canonical_id(raw_id) or "ID")
            self.session_table.setCellWidget(row, S_CORRECTED, edit)
            self.session_edits[folder.name] = edit
            saved_date = self.work.fixes.dates.get(folder.name)
            date_edit = QLineEdit(saved_date.strftime(ISO_DATE) if saved_date else "")
            date_edit.setValidator(DateValidator(date_edit))
            date_edit.setPlaceholderText(when.split(" ")[0] if when != "-" else "YYYY-MM-DD")
            date_edit.setToolTip(
                "The real scan date, if the folder name's is wrong (YYYY-MM-DD). Sessions are "
                "numbered and logs matched by it."
            )
            self.session_table.setCellWidget(row, S_DATE, date_edit)
            self.folder_date_edits[folder.name] = date_edit
            session_edit = QLineEdit(self.work.fixes.sessions.get(folder.name, ""))
            session_edit.setPlaceholderText("by date")
            session_edit.setValidator(session_validator(session_edit))
            session_edit.setToolTip(
                "Set this folder's BIDS session number (e.g. 2 when the first visit is "
                "missing). Empty: numbered by date. The subject's other sessions are numbered "
                "around it."
            )
            self.session_table.setCellWidget(row, S_FIXED, session_edit)
            self.folder_session_edits[folder.name] = session_edit
        return self.session_table

    def _build_detail(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.detail_title = QLabel("Select a log above.")
        self.detail_title.setWordWrap(True)
        self.detail_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail_title)
        self.fit_label = QLabel()
        self.fit_label.setWordWrap(True)
        layout.addWidget(self.fit_label)

        actions = QHBoxLayout()
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Note (optional), e.g. why it's an error")
        actions.addWidget(self.note_edit, 1)
        self.ok_button = QPushButton("Mark OK")
        self.ok_button.setToolTip("Looked at it: the issues are acceptable, use it.")
        self.ok_button.clicked.connect(lambda: self.set_verdict(REVIEW_OK))
        self.error_button = QPushButton("Mark error")
        self.error_button.setToolTip("Unusable: leave it out of BIDS.")
        self.error_button.clicked.connect(lambda: self.set_verdict(REVIEW_ERROR))
        self.use_button = QPushButton("Use this one")
        self.use_button.setToolTip(
            "Of several logs for this subject, task and day: put this one into BIDS and mark "
            "the others 'not used'."
        )
        self.use_button.clicked.connect(self.use_this_one)
        self.clear_button = QPushButton("Clear verdict")
        self.clear_button.clicked.connect(lambda: self.set_verdict(None))
        for button in (self.ok_button, self.error_button, self.use_button, self.clear_button):
            actions.addWidget(button)
        self.unmark_button = QPushButton("Un-mark crosschecked")
        self.unmark_button.setToolTip("Take the reviewed mark off the selected log(s).")
        self.unmark_button.clicked.connect(lambda: self.set_crosschecked(False))
        actions.addWidget(self.unmark_button)
        self.mark_button = mark_button(
            "Mark crosschecked",
            "You have been through the selected log(s): mark them as reviewed. Select several "
            "rows (Ctrl/Shift-click or drag) to mark them all at once.",
        )
        self.mark_button.clicked.connect(lambda: self.set_crosschecked(True))
        actions.addWidget(self.mark_button)
        layout.addLayout(actions)

        events = QSplitter(Qt.Orientation.Horizontal)
        self.events_summary = QTextEdit()
        self.events_summary.setReadOnly(True)
        self.events_summary.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.events_summary.setToolTip(
            "Trial types with their event count and total time, then the behavioural "
            "column per trial type. Ratings are ordinal: how often each value occurs, the "
            "median and the range (no means)."
        )
        events.addWidget(self.events_summary)
        self.events_table = QTableWidget(0, 4)
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        events.addWidget(self.events_table)
        events.setSizes([550, 650])
        layout.addWidget(events, 1)
        return panel

    # ---- matching -------------------------------------------------------------------------

    def typed_corrections(self) -> dict[str, tuple[str, dt.date | None]] | None:
        """Typed IDs and dates by log; None (bad cells marked red) if a date won't parse."""
        result: dict[str, tuple[str, dt.date | None]] = {}
        ok = True
        for info, id_edit, date_edit in zip(
            self.infos, self.id_edits, self.date_edits, strict=True
        ):
            try:
                day = parse_date(date_edit.text())
                date_edit.setStyleSheet("")
            except ValueError:
                date_edit.setStyleSheet(f"background-color: {BAD_COLOR};")
                ok = False
                continue
            result[info.key] = (id_edit.text(), day)
        return result if ok else None

    def dicom_corrections(self) -> dict[str, str]:
        """Typed DICOM folder ID corrections: {folder name: ID}."""
        return {
            name: edit.text().strip()
            for name, edit in self.session_edits.items()
            if edit.text().strip()
        }

    def dicom_dates(self) -> dict[str, dt.date] | None:
        """Typed DICOM folder scan dates; None (bad cells marked red) if one is unfinished
        (the box only lets real YYYY-MM-DD dates be typed)."""
        dates, ok = {}, True
        for name, edit in self.folder_date_edits.items():
            text = edit.text().strip()
            if text and not edit.hasAcceptableInput():
                edit.setStyleSheet(f"background-color: {BAD_COLOR};")
                ok = False
                continue
            edit.setStyleSheet("")
            if text:
                dates[name] = dt.date.fromisoformat(text)
        return dates if ok else None

    def dicom_sessions(self) -> dict[str, str] | None:
        """Typed session numbers; None (bad cells marked red) if one isn't a whole number
        from 1 (the box only lets digits be typed)."""
        labels, ok = {}, True
        for name, edit in self.folder_session_edits.items():
            text = edit.text().strip()
            if text and not (edit.hasAcceptableInput() and text.isdigit()):
                edit.setStyleSheet(f"background-color: {BAD_COLOR};")
                ok = False
                continue
            edit.setStyleSheet("")
            if text:
                labels[name] = str(int(text))
        return labels if ok else None

    def take_typed(self) -> bool:
        """Put what is typed into the work; False (bad cells marked red) if some is wrong."""
        corrections = self.typed_corrections()
        dates, sessions = self.dicom_dates(), self.dicom_sessions()
        if corrections is None or dates is None or sessions is None:
            return False
        self.work.set_corrections(corrections)
        fixes = self.work.fixes
        fixes.ids, fixes.dates, fixes.sessions = self.dicom_corrections(), dates, sessions
        return True

    def recheck(self) -> None:
        if not self.take_typed():
            self.set_status(FIX_RED)
            return
        self.recheck_button.setEnabled(False)
        self.sessions_recheck_button.setEnabled(False)
        self.progress_bar.setRange(0, len(self.infos))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        try:
            self.work.match()
            self.show_matches(progress=True)
            self.show_detail()
        finally:
            self.progress_bar.setVisible(False)
            self.recheck_button.setEnabled(True)
            self.sessions_recheck_button.setEnabled(True)
        self.sessions_status.setText("Re-checked (not saved yet)")

    def show_matches(self, progress: bool = False) -> None:
        for row, m in enumerate(self.matches):
            if progress and row % ROWS_PER_PROGRESS_STEP == 0:
                self.progress_bar.setValue(row)
                QApplication.processEvents()  # let the bar move; Re-check is disabled meanwhile
            info = m.info
            review = info.correction.review or ""
            cells = log_cells(m)
            verdict = cells[VERDICT]
            for column, text in cells.items():
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == MATCH:
                    item.setForeground(QColor(OK_COLOR if m.status == MATCHED else BAD_COLOR))
                if column == STATE and info.state != COMPLETE:
                    item.setForeground(QColor(WARN_COLOR))
                if column == VERDICT and verdict:
                    color = VERDICT_COLORS[review] if review else CROSSCHECKED_COLOR
                    item.setForeground(QColor(color))
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.show_sessions()
        self.summary.setText(self.work.summary())
        self.apply_filter()

    def show_sessions(self) -> None:
        """Refresh the Raw DICOMs tab: resolved ID, BIDS session and matched logs."""
        for row in range(len(self.scanned)):
            cells = self.work.folder_cells(row)
            for column, text in cells.items():
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == S_NOTES and text:
                    item.setForeground(QColor(WARN_COLOR))
                if column == S_CHECKED:
                    item.setForeground(QColor(CROSSCHECKED_COLOR))
                    item.setToolTip("crosschecked" if text else "")
                self.session_table.setItem(row, column, item)
        self.session_table.resizeColumnsToContents()
        self.session_table.horizontalHeader().setStretchLastSection(True)

    def selected_folders(self) -> list[str]:
        """The DICOM folder names selected on the Raw DICOMs tab."""
        rows = sorted(i.row() for i in self.session_table.selectionModel().selectedRows())
        return [self.scanned[row][0].name for row in rows]

    def update_session_buttons(self) -> None:
        chosen = self.selected_folders()
        n = len(chosen)
        self.mark_sessions_button.setEnabled(any(f not in self.work.fixes.checked for f in chosen))
        self.mark_sessions_button.setText(
            f"Mark {n} crosschecked" if n > 1 else "Mark crosschecked"
        )
        self.unmark_sessions_button.setEnabled(any(f in self.work.fixes.checked for f in chosen))
        checked = len(self.work.fixes.checked & {f.name for f, _ in self.scanned})
        self.sessions_selected_label.setText(
            f"{n} selected; {CROSSCHECKED_ICON} {checked} of {len(self.scanned)} crosschecked"
        )

    def set_folders_checked(self, marked: bool) -> None:
        """Mark (or un-mark) every selected DICOM folder as reviewed; kept on Save."""
        folders = set(self.selected_folders())
        if marked:
            self.work.fixes.checked |= folders
        else:
            self.work.fixes.checked -= folders
        self.show_sessions()
        self.update_session_buttons()

    def apply_filter(self) -> None:
        only = self.problems_only.isChecked()
        for row, m in enumerate(self.matches):
            self.table.setRowHidden(row, only and not needs_review(m))

    # ---- selected log ---------------------------------------------------------------------

    def selected_matches(self) -> list[LogMatch]:
        """Every selected log still shown (not hidden by 'Needs review only')."""
        rows = sorted(index.row() for index in self.table.selectionModel().selectedRows())
        return [self.matches[row] for row in rows if not self.table.isRowHidden(row)]

    def selected(self) -> LogMatch | None:
        """The one selected log; None when none or several are selected."""
        chosen = self.selected_matches()
        return chosen[0] if len(chosen) == 1 else None

    def update_mark_buttons(self, chosen: list[LogMatch]) -> None:
        n = len(chosen)
        self.mark_button.setEnabled(any(not m.info.correction.crosschecked for m in chosen))
        self.mark_button.setText(f"Mark {n} crosschecked" if n > 1 else "Mark crosschecked")
        self.unmark_button.setEnabled(any(m.info.correction.crosschecked for m in chosen))

    def show_detail(self) -> None:
        chosen = self.selected_matches()
        m = chosen[0] if len(chosen) == 1 else None
        for button in (self.ok_button, self.error_button, self.use_button, self.clear_button):
            button.setEnabled(m is not None)
        self.update_mark_buttons(chosen)
        self.events_table.setRowCount(0)
        self.events_summary.clear()
        self.fit_label.setText("")
        if len(chosen) > 1:
            self.detail_title.setText(
                f"<b>{len(chosen)} logs selected</b> -- mark them crosschecked together, or "
                "select one to see its events."
            )
            return
        if m is None:
            self.detail_title.setText("Select a log above.")
            return
        info = m.info
        self.use_button.setEnabled(bool(same_day_rivals(self.matches, m)))
        self.note_edit.setText(info.correction.note)
        inventory = self.work.inventory_of(m) or "Scan inventory: no row for this session"
        self.detail_title.setText(f"<b>{info.key}</b> -- {log_title(m)}<br>{escape(inventory)}")
        events = self.work.events_of(info)
        if isinstance(events, str):
            self.fit_label.setText(events)
            self.fit_label.setStyleSheet(f"color: {BAD_COLOR};")
            return
        text, color = self.work.fit_of(m, events)
        self.fit_label.setText(text)
        self.fit_label.setStyleSheet(f"color: {color};")
        self.events_summary.setPlainText("\n".join(events_summary(events)))
        self.events_table.setColumnCount(4)
        self.events_table.setHorizontalHeaderLabels(
            ["onset (s)", "duration (s)", "trial_type", events.modulator]
        )
        rows = event_rows(events)
        self.events_table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, text in enumerate(values):
                self.events_table.setItem(row, column, QTableWidgetItem(text))
        self.events_table.resizeColumnsToContents()

    def set_verdict(self, review: str | None) -> None:
        m = self.selected()
        if m is None:
            return
        note = self.note_edit.text().strip() if review else ""
        self.work.set_verdict(m.info.key, review, note)
        self.recheck()

    def set_crosschecked(self, marked: bool) -> None:
        """Mark (or un-mark) every selected log as reviewed; kept on Save."""
        keys = {m.info.key for m in self.selected_matches()}
        if not keys:
            return
        if marked:
            self.work.crosschecked |= keys
        else:
            self.work.crosschecked -= keys
        self.recheck()

    def use_this_one(self) -> None:
        m = self.selected()
        if m is None:
            return
        self.work.use_this_one(m, self.note_edit.text().strip())
        self.recheck()

    def export_destinations(self) -> dict[str, str]:
        """What the export can go into: {text shown: INTO_BIDS / INTO_OUTPUT / INTO_OTHER}."""
        return self.work.export_destinations()

    def export_target(self) -> tuple[Path, str, bool] | None:
        """Ask where and how: (folder, layout, into the BIDS folder); None if cancelled."""
        destinations = self.export_destinations()
        where, ok = QInputDialog.getItem(
            self, "Export logs", "Put the events files in:", list(destinations), 0, False
        )
        if not ok:
            return None
        destination = destinations[where]
        if destination == INTO_BIDS:
            replace = QMessageBox.question(
                self,
                "Export into the BIDS folder",
                EXPORT_INTO_BIDS_QUESTION,
            )
            if replace != QMessageBox.StandardButton.Yes:
                return None
            return self.bids_root, BIDS_LAYOUT, True
        choice, ok = QInputDialog.getItem(
            self, "Export logs", "Lay the events files out as:", list(EXPORT_LAYOUTS), 0, False
        )
        if not ok:
            return None
        if destination == INTO_OUTPUT and self.work.output_root is not None:
            out_root = self.work.output_root / EXPORT_SUBFOLDER
        else:
            chosen = QFileDialog.getExistingDirectory(
                self, "Export the events files to", str(self.bids_root.parent)
            )
            if not chosen:
                return None
            out_root = Path(chosen)
        if problem := self.work.export_problem(out_root):
            QMessageBox.warning(self, "Pick another folder", problem)
            return None
        return out_root, EXPORT_LAYOUTS[choice], False

    def on_export(self) -> None:
        """Save as: pick where and how, then write every log's events.tsv there."""
        self.recheck()  # export what the table shows, with the corrections typed so far
        target = self.export_target()
        if target is None:
            return
        out_root, layout, into_bids = target
        self.export_button.setEnabled(False)
        self.progress_bar.setFormat("Exporting %v / %m logs")
        self.progress_bar.setVisible(True)

        def progress(done: int, total: int) -> None:
            self.progress_bar.setRange(0, max(total, 1))
            self.progress_bar.setValue(done)
            QApplication.processEvents()

        try:
            report = self.work.export(out_root, layout, into_bids, progress)
        finally:
            self.progress_bar.setVisible(False)
            self.progress_bar.setFormat("Re-checking %v / %m logs")
            self.export_button.setEnabled(True)
        box = QMessageBox.warning if report.failed else QMessageBox.information
        box(self, "Logs exported", export_text(report, out_root, into_bids))

    def save(self) -> None:
        if not self.take_typed():
            self.set_status(f"Not saved: {FIX_RED[0].lower()}{FIX_RED[1:]}")
            return
        self.work.save()
        self.summary.setText(self.summary.text() + "  (saved)")
        self.sessions_status.setText("Saved")
        self.saved.emit()

    def set_status(self, text: str) -> None:
        """A message on both tabs (e.g. a cell to fix may be on the other one)."""
        self.summary.setText(text)
        self.sessions_status.setText(text)


class BehavLogsPage(QWidget):
    """The ID & Behaviour Logs tab. Reading every log and DICOM folder takes a while, so it
    happens on a worker thread with a progress bar: as soon as the toolbox opens (the tab is
    ready when you get to it), and again when a folder changes or on Reload."""

    badge_changed = Signal(str)
    panel_changed = Signal()  # loaded or dropped: the Raw DICOMs tab shows its part
    load_progress = Signal(int, int, str)  # (done, total, what), while reading
    logs_loaded = Signal(object)  # list[LogInfo] read ([] when dropped), for Scan lengths

    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        self.panel: BehavLogsPanel | None = None
        self.loader: BehavLoadThread | None = None
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.status = QLabel()
        row.addWidget(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        row.addWidget(self.progress_bar, 1)
        row.addStretch(1)
        self.load_button = QPushButton("Reload logs")
        self.load_button.setToolTip(
            "Read every behavioural log and DICOM session folder again. Drops corrections "
            "typed but not saved."
        )
        self.load_button.clicked.connect(self.load)
        row.addWidget(self.load_button)
        layout.addLayout(row)
        self.banner = NoticeBanner()
        layout.addWidget(self.banner)
        self.filler = QWidget()  # keeps the row at the top until the logs are loaded
        layout.addWidget(self.filler, 1)
        self.layout_ = layout
        state.folder_changed.connect(self.on_folder_changed)
        self.update_status()

    def folders(self) -> tuple[Path, Path, Path] | None:
        return behav_folders(self.state)

    def update_status(self, text: str = "") -> None:
        ready = self.folders() is not None
        self.load_button.setEnabled(ready and self.loader is None)
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.banner.set_messages(self.notices())

    def notices(self) -> list[str]:
        """Folders not chosen; once read, no logs or no DICOM sessions found in them."""
        if missing := missing_folders_notice(self.state):
            return missing
        return self.panel.work.notices() if self.panel is not None else []

    def show_page(self) -> None:
        if self.panel is None and self.loader is None and self.folders() is not None:
            self.load()

    def on_folder_changed(self, key: str) -> None:
        if key == "output" and self.panel is not None:  # only the export uses it
            self.panel.work.output_root = self.state.folder("output")
        if key in ("bids", "behav", "dicom"):
            self.drop_panel()
            if self.folders() is not None:
                self.load()  # in the background: ready by the time you open the tab
        self.update_status()

    def drop_panel(self) -> None:
        if self.panel is not None:
            self.layout_.removeWidget(self.panel)
            # The Raw DICOMs tab holds this part: it isn't deleted with the panel.
            self.panel.sessions_view.setParent(None)
            self.panel.sessions_view.deleteLater()
            self.panel.deleteLater()
            self.panel = None
            self.filler.show()
            self.badge_changed.emit("")
            self.panel_changed.emit()
            self.logs_loaded.emit([])

    def load(self) -> None:
        """Read the logs on a worker thread; the panel appears when it is done."""
        folders = self.folders()
        if folders is None:
            return
        if self.loader is not None:  # a folder changed mid-load: read again once it ends
            self.loader.done.disconnect()
            self.loader.done.connect(lambda _result: self.load())
            return
        self.drop_panel()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("Finding logs...")
        self.progress_bar.setVisible(True)
        self.loader = BehavLoadThread(*folders)
        self.loader.progress.connect(self.on_progress)
        self.loader.done.connect(self.on_loaded)
        self.loader.start()
        self.update_status()

    def on_progress(self, done: int, total: int, what: str) -> None:
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)
        self.progress_bar.setFormat(f"{what}: %v / %m")
        self.load_progress.emit(done, total, what)

    def on_loaded(self, result: BehavData | str) -> None:
        if self.loader is not None:
            self.loader.wait()
        self.loader = None
        self.progress_bar.setVisible(False)
        folders = self.folders()
        if isinstance(result, str) or folders is None:
            self.update_status(f"Can't read the logs: {result}" if isinstance(result, str) else "")
            return
        self.panel = BehavLogsPanel(
            *folders, self, data=result, output_root=self.state.folder("output")
        )
        self.panel.saved.connect(self.on_saved)
        if VIEW_ONLY in self.state.busy:  # corrections feed Convert to BIDS
            self.panel.work.bids_locked = True
            for button in (self.panel.save_button, self.panel.sessions_save_button):
                button.setEnabled(False)
                button.setToolTip("Locked: view-only review package.")
        self.filler.hide()
        self.layout_.addWidget(self.panel, 1)
        self.update_status()
        self.badge_changed.emit(self.badge())
        self.panel_changed.emit()
        self.logs_loaded.emit(self.panel.infos)

    def wait(self) -> None:
        """Let a running read finish (closing the window, or a test)."""
        if self.loader is not None:
            self.loader.wait()
            QApplication.processEvents()  # delivers its `done`

    def badge(self) -> str:
        waiting = self.panel.work.waiting() if self.panel else 0
        return f"{waiting} to review" if waiting else ""

    def on_saved(self) -> None:
        self.badge_changed.emit(self.badge())
        if self.panel is not None:  # verdicts change which logs count for Scan lengths
            self.logs_loaded.emit(self.panel.infos)
        QMessageBox.information(
            self, "Saved", "Corrections saved. Convert to BIDS (tab 1) to apply them."
        )


class ScanSessionsPage(QWidget):
    """The Raw DICOMs tab: the DICOM session folders part of the Behaviour logs tab's
    panel (one load, one Save for both), shown here on a tab of its own."""

    def __init__(self, behav: BehavLogsPage) -> None:
        super().__init__()
        self.behav = behav
        self.view: QWidget | None = None
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.status = QLabel()
        row.addWidget(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        row.addWidget(self.progress_bar, 1)
        row.addStretch(1)
        self.load_button = QPushButton("Reload")
        self.load_button.setToolTip(
            "Read every DICOM session folder and behavioural log again. Drops corrections "
            "typed but not saved."
        )
        self.load_button.clicked.connect(behav.load)
        row.addWidget(self.load_button)
        layout.addLayout(row)
        self.banner = NoticeBanner()
        layout.addWidget(self.banner)
        self.filler = QWidget()
        layout.addWidget(self.filler, 1)
        self.layout_ = layout
        behav.panel_changed.connect(self.on_panel_changed)
        behav.load_progress.connect(self.on_progress)
        behav.state.folder_changed.connect(lambda _key: self.update_status())
        self.on_panel_changed()

    def show_page(self) -> None:
        self.behav.show_page()  # starts reading if it hasn't yet
        self.update_status()

    def on_progress(self, done: int, total: int, what: str) -> None:
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)
        self.progress_bar.setFormat(f"{what}: %v / %m")
        self.update_status()

    def on_panel_changed(self) -> None:
        if self.view is not None:
            self.layout_.removeWidget(self.view)
            self.view = None
        panel = self.behav.panel
        if panel is not None:
            self.view = panel.sessions_view
            self.layout_.addWidget(self.view, 1)
        self.filler.setVisible(self.view is None)
        self.update_status()

    def update_status(self) -> None:
        loading = self.behav.loader is not None
        self.progress_bar.setVisible(loading)
        self.status.setText(self.behav.status.text())
        self.status.setVisible(bool(self.status.text()))
        self.load_button.setEnabled(self.behav.load_button.isEnabled())
        self.banner.set_messages(self.behav.notices())
