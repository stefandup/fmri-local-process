"""IDs & behaviour logs: match each log to a scan session, look at its events, record a verdict.

* A wrong ID or date in a log's name is corrected here; so is a wrong subject ID in a DICOM
  session folder's name (Scan sessions tab: each folder with its scan date/time and the ID
  as written, e.g. a twin's 12345-1, which becomes 12345T1).
* Selecting a log shows the events it produces (and whether they fit the converted BOLD run),
  so it can be marked OK or as an error.
* Of several logs on one day, "Use this one" picks the log that goes into BIDS.

Everything is saved in the BIDS folder (code/behav_corrections.json and
code/dicom_id_corrections.json), never by renaming or editing the raw files (vr_lab_toolbox's
rule). Convert / update BIDS applies it.
"""

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field
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

from fmri_local_process.gui.image_view import HelpToggle
from fmri_local_process.gui.qt_common import paths_conflict
from fmri_local_process.gui.toolbox_state import VIEW_ONLY, ToolboxState
from fmri_local_process.processing.behav_export import (
    BIDS_LAYOUT,
    ONE_FOLDER,
    UNPLACED_DIR,
    export_events,
)
from fmri_local_process.processing.behav_logs import (
    BEHAV_TASKS,
    EventTable,
    canonical_id,
    id_problem,
    read_log_lines,
)
from fmri_local_process.processing.behav_match import (
    COMPLETE,
    CORRECTIONS_FILE,
    ISO_DATE,
    MATCHED,
    REVIEW_ERROR,
    REVIEW_NOT_USED,
    REVIEW_OK,
    Correction,
    LogInfo,
    LogMatch,
    SessionInfo,
    events_summary,
    inspect_logs,
    inventory,
    match_logs,
    needs_review,
    read_corrections,
    same_day_rivals,
    write_corrections,
)
from fmri_local_process.processing.crosscheck import bold_run_length
from fmri_local_process.processing.philani_bids import (
    DATE_FORMAT,
    DICOM_CORRECTIONS_FILE,
    ScannedFolder,
    read_dicom_corrections,
    read_dicom_date_corrections,
    read_dicom_session_corrections,
    read_excluded_sessions,
    read_scan_rules,
    scan_dicom_root,
    session_infos,
    write_dicom_corrections,
    write_dicom_date_corrections,
    write_dicom_session_corrections,
)

OK_COLOR = "#2ecc71"
WARN_COLOR = "#f39c12"
BAD_COLOR = "#e74c3c"
COLUMNS = (
    "Log", "Task", "ID in name", "Logged", "Corrected ID", "Corrected date",
    "Verdict", "Match", "Session", "Log state", "Notes",
)  # fmt: skip
CORRECTED_ID, CORRECTED_DATE, VERDICT, MATCH, SESSION, STATE, NOTES = range(4, 11)
VERDICT_COLORS = {REVIEW_OK: OK_COLOR, REVIEW_ERROR: BAD_COLOR, REVIEW_NOT_USED: WARN_COLOR}
SESSION_COLUMNS = (
    "DICOM folder", "Scanned", "ID in folder", "Corrected ID", "Corrected date",
    "Set session", "Resolves to", "BIDS session", "Scans", "Logs", "Notes",
)  # fmt: skip
S_CORRECTED, S_DATE, S_FIXED, S_RESOLVED, S_SESSION, S_TASKS, S_LOGS, S_NOTES = range(3, 11)

Verdict = tuple[str, str]  # (review, note)


ISO_PATTERN = re.compile(r"\d{0,4}(-\d{0,2}(-\d{0,2})?)?")  # YYYY-MM-DD as it is typed
MAX_SESSION = 99


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


def parse_date(text: str) -> dt.date | None:
    """'' -> None; 2024-04-03 or 20240403 -> date; anything else raises ValueError."""
    text = text.strip()
    if not text:
        return None
    for fmt in (ISO_DATE, "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{text!r} is not a date (YYYY-MM-DD)")


def folder_name_parts(name: str) -> tuple[str, str]:
    """(scan date/time, ID as written) from <YYYYMMDD>_<HHMM>_<ID>[...]; "-" where missing."""
    parts = name.split("_")
    try:
        day = dt.datetime.strptime(parts[0], DATE_FORMAT).strftime(ISO_DATE)
    except ValueError:
        day = "-"
    time = parts[1] if len(parts) > 1 and len(parts[1]) == 4 and parts[1].isdigit() else ""
    when = f"{day} {time[:2]}:{time[2:]}" if time and day != "-" else day
    return when, parts[2] if len(parts) > 2 else "-"


def id_text(raw: str, canonical: str) -> str:
    return raw if raw == canonical else f"{raw} -> {canonical}"


def inventory_text(infos: list[LogInfo]) -> str:
    lines = []
    for task, inv in inventory(infos).items():
        states = ", ".join(f"{n} {state}" for state, n in inv.states.most_common())
        lines.append(f"{task}: {inv.logs} log(s) -- {states}")
        types = ", ".join(f"{t} ({n})" for t, n in inv.trial_types.most_common())
        lines.append(f"  trial types (logs containing each): {types or '-'}")
        for n, (layout, count) in enumerate(inv.layouts.most_common(), start=1):
            lines.append(f"  column layout {n}: {count} log(s), {len(layout)} columns: ")
            lines.append("    " + ", ".join(layout))
        lines.append("")
    return "\n".join(lines)


def events_end(table: EventTable) -> float | None:
    """When the last event ends, in seconds from the first pulse."""
    if not table.onset or table.duration[-1] is None:
        return None
    return table.onset[-1] + table.duration[-1]


def fit_text(table: EventTable, run: tuple[int, float | None] | None) -> tuple[str, str]:
    """How the events line up with the converted BOLD run: (text, colour)."""
    end = events_end(table)
    end_text = f"events end at {end:.1f} s" if end is not None else "events have no end"
    if run is None:
        return f"{end_text}; the BOLD run isn't in the BIDS folder yet", WARN_COLOR
    volumes, tr = run
    if tr is None:
        return f"{end_text}; BOLD run has {volumes} volumes (no TR in its sidecar)", WARN_COLOR
    length = volumes * tr
    text = f"{end_text}; BOLD run is {volumes} volumes x {tr:g} s = {length:.1f} s"
    if end is not None and end > length + tr:
        return f"{text}: events run past the end of the scan", BAD_COLOR
    return text, OK_COLOR


@dataclass
class BehavData:
    """What the tab reads from disk -- the slow part, done on a worker thread."""

    infos: list[LogInfo]
    scanned: list[ScannedFolder]
    dicom_corrections: dict[str, str]
    excluded: set[str]  # DICOM folders deleted at crosscheck
    dicom_dates: dict[str, dt.date] = field(default_factory=dict)  # corrected scan dates
    dicom_sessions: dict[str, str] = field(default_factory=dict)  # session numbers set


LoadProgress = Callable[[int, int, str], None]  # (done, total, what)


def load_behav_data(
    bids_root: Path, behav_root: Path, dicom_root: Path, progress: LoadProgress | None = None
) -> BehavData:
    def step(what: str) -> Callable[[int, int], None] | None:
        if progress is None:
            return None
        report = progress
        return lambda done, total: report(done, total, what)

    infos = inspect_logs(behav_root, read_corrections(bids_root), step("Reading logs"))
    scanned = scan_dicom_root(
        dicom_root, step("Scanning DICOM session folders"), read_scan_rules(bids_root)
    )
    return BehavData(
        infos,
        scanned,
        read_dicom_corrections(bids_root),
        set(read_excluded_sessions(bids_root)),
        read_dicom_date_corrections(bids_root),
        read_dicom_session_corrections(bids_root),
    )


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


HELP_TEXT = (
    "Each log is matched to a scan session by subject ID and date. A twin's 12345-1 becomes "
    "12345T1; other characters (e.g. a space) are removed and flagged.<br><br>"
    "Correct a wrong ID or date (<b>Behaviour logs</b> tab) or a wrong subject ID in a DICOM "
    "folder name (<b>Scan sessions</b> tab: scan date and time shown, to tell twins apart) "
    "and press <b>Re-check</b>. Select a log to see its events and mark it OK or as an error; "
    "of several logs on one day, pick the one to use.<br><br>"
    "<b>Save</b> keeps all of this in the BIDS folder (code/behav_corrections.json and "
    "code/dicom_id_corrections.json), and Convert to BIDS applies it. Raw files are never "
    "renamed. Sessions already converted under an old ID stay in the BIDS folder until you "
    "remove them."
)
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
    ):
        super().__init__(parent)
        self.bids_root, self.behav_root, self.dicom_root = bids_root, behav_root, dicom_root
        self.events_cache: dict[str, EventTable | str] = {}  # key -> table, or why it failed
        self.behav_tasks = {t.task for t in BEHAV_TASKS}  # tasks that should have a log

        if data is None:  # read here and now (the tab reads it on a worker thread instead)
            QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                data = load_behav_data(bids_root, behav_root, dicom_root)
            finally:
                QGuiApplication.restoreOverrideCursor()
        self.infos, self.scanned = data.infos, data.scanned
        self.excluded = data.excluded
        dicom_corrections = data.dicom_corrections
        self.saved_dates, self.saved_sessions = data.dicom_dates, data.dicom_sessions
        self.sessions: list[SessionInfo] = session_infos(
            self.scanned, dicom_corrections, self.excluded, data.dicom_dates, data.dicom_sessions
        )
        self.verdicts: dict[str, Verdict] = {
            info.key: (info.correction.review, info.correction.note)
            for info in self.infos
            if info.correction.review
        }

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
            "Save every log as an events.tsv in a folder you choose (all in one folder, or in "
            "BIDS sub-/ses-/func folders), using the corrections and verdicts here. No scans "
            "are converted; logs marked error or not used are left out."
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

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.top_tabs = QTabWidget()
        self.top_tabs.addTab(self._build_table(), "Behaviour logs")
        self.top_tabs.addTab(self._build_session_table(dicom_corrections), "Scan sessions")
        splitter.addWidget(self.top_tabs)
        tabs = QTabWidget()
        tabs.addTab(self._build_detail(), "Selected log")
        self.contents = QTextEdit()
        self.contents.setReadOnly(True)
        self.contents.setPlainText(inventory_text(self.infos))
        tabs.addTab(self.contents, "Contents per task")
        splitter.addWidget(tabs)
        splitter.setSizes([450, 400])
        layout.addWidget(splitter, 1)
        self.show_matches(match_logs(self.infos, self.sessions))
        self.show_detail()

    # ---- layout -------------------------------------------------------------------------

    def _build_table(self) -> QTableWidget:
        self.table = QTableWidget(len(self.infos), len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
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

    def _build_session_table(self, corrections: dict[str, str]) -> QTableWidget:
        """Every DICOM session folder: when it was scanned, its ID as written, a correction."""
        self.session_table = QTableWidget(len(self.scanned), len(SESSION_COLUMNS))
        self.session_table.setHorizontalHeaderLabels(list(SESSION_COLUMNS))
        self.session_table.verticalHeader().setVisible(False)
        self.session_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.session_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
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
            edit = QLineEdit(corrections.get(folder.name, ""))
            edit.setPlaceholderText(canonical_id(raw_id) or "ID")
            self.session_table.setCellWidget(row, S_CORRECTED, edit)
            self.session_edits[folder.name] = edit
            saved_date = self.saved_dates.get(folder.name)
            date_edit = QLineEdit(saved_date.strftime(ISO_DATE) if saved_date else "")
            date_edit.setValidator(DateValidator(date_edit))
            date_edit.setPlaceholderText(when.split(" ")[0] if when != "-" else "YYYY-MM-DD")
            date_edit.setToolTip(
                "The real scan date, if the folder name's is wrong (YYYY-MM-DD). Sessions are "
                "numbered and logs matched by it."
            )
            self.session_table.setCellWidget(row, S_DATE, date_edit)
            self.folder_date_edits[folder.name] = date_edit
            session_edit = QLineEdit(self.saved_sessions.get(folder.name, ""))
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

    def corrections(self) -> dict[str, Correction] | None:
        """Typed corrections + verdicts; None (bad cells marked red) if a date won't parse."""
        result: dict[str, Correction] = {}
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
            review, note = self.verdicts.get(info.key, (None, ""))
            correction = Correction(id_edit.text().strip() or None, day, review, note)
            if correction != Correction():
                result[info.key] = correction
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

    def recheck(self) -> None:
        corrections = self.corrections()
        dates, sessions = self.dicom_dates(), self.dicom_sessions()
        if corrections is None or dates is None or sessions is None:
            self.summary.setText(
                "Fix the cells marked red (dates as YYYY-MM-DD, sessions as a number)."
            )
            return
        self.recheck_button.setEnabled(False)
        self.progress_bar.setRange(0, len(self.infos))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        try:
            for info in self.infos:
                info.correction = corrections.get(info.key, Correction())
            self.sessions = session_infos(
                self.scanned, self.dicom_corrections(), self.excluded, dates, sessions
            )
            self.show_matches(match_logs(self.infos, self.sessions), progress=True)
            self.show_detail()
        finally:
            self.progress_bar.setVisible(False)
            self.recheck_button.setEnabled(True)

    def show_matches(self, matches: list[LogMatch], progress: bool = False) -> None:
        self.matches = matches
        for row, m in enumerate(matches):
            if progress and row % ROWS_PER_PROGRESS_STEP == 0:
                self.progress_bar.setValue(row)
                QApplication.processEvents()  # let the bar move; Re-check is disabled meanwhile
            info = m.info
            logged = f"{info.time_text} ({info.date_source})" if info.found_date else "-"
            review = info.correction.review or ""
            cells = {
                0: info.key,
                1: info.task.task if info.task else "?",
                2: id_text(info.raw_subject, info.file_subject),
                3: logged,
                VERDICT: review,
                MATCH: m.status,
                SESSION: f"{m.session.name} ({m.session.when})" if m.session else "",
                STATE: info.state,
                NOTES: "; ".join(filter(None, [m.hint, *info.notes, info.correction.note])),
            }
            for column, text in cells.items():
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == MATCH:
                    item.setForeground(QColor(OK_COLOR if m.status == MATCHED else BAD_COLOR))
                if column == STATE and info.state != COMPLETE:
                    item.setForeground(QColor(WARN_COLOR))
                if column == VERDICT and review:
                    item.setForeground(QColor(VERDICT_COLORS[review]))
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.show_sessions()
        matched = sum(m.status == MATCHED for m in matches)
        waiting = sum(needs_review(m) for m in matches)
        self.summary.setText(
            f"{matched} of {len(matches)} log(s) matched to a session; {waiting} need review"
        )
        self.apply_filter()

    def show_sessions(self) -> None:
        """Refresh the Scan sessions tab: resolved ID, BIDS session and matched logs."""
        by_folder = {s.folder: s for s in self.sessions}
        logs: dict[str, list[str]] = {}
        for m in self.matches:
            if m.status == MATCHED and m.session is not None and m.info.task is not None:
                logs.setdefault(m.session.folder, []).append(
                    f"{m.info.task.task}: {m.info.time_text}"
                )
        for row, (folder, tasks) in enumerate(self.scanned):
            session = by_folder.get(folder.name)
            _, raw_id = folder_name_parts(folder.name)
            matched = logs.get(folder.name, [])
            missing = sorted(t for t in tasks if not any(x.startswith(f"{t}:") for x in matched))
            notes = [id_problem(raw_id)] if not self.session_edits[folder.name].text() else []
            if session is None:
                notes.append(
                    "not converted: no date or ID in the folder name; give it a Corrected ID "
                    "and a Corrected date, then Re-check and Save"
                )
            fixed = self.folder_session_edits[folder.name].text().strip()
            if session is not None and fixed and session.label != fixed:
                notes.append(f"ses-{fixed} already set for another of its folders")
            if not tasks:
                notes.append(
                    "no series matches a scan rule: nothing would be converted (the "
                    "conversion summary lists its series folders)"
                )
            notes += [f"no {task} log" for task in missing if task in self.behav_tasks]
            cells = {
                S_RESOLVED: id_text(raw_id, session.subject) if session else "-",
                S_SESSION: session.name if session else "",
                S_LOGS: "; ".join(sorted(matched)),
                S_NOTES: "; ".join(filter(None, notes)),
            }
            for column, text in cells.items():
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == S_NOTES and text:
                    item.setForeground(QColor(WARN_COLOR))
                self.session_table.setItem(row, column, item)
        self.session_table.resizeColumnsToContents()
        self.session_table.horizontalHeader().setStretchLastSection(True)

    def apply_filter(self) -> None:
        only = self.problems_only.isChecked()
        for row, m in enumerate(self.matches):
            self.table.setRowHidden(row, only and not needs_review(m))

    # ---- selected log ---------------------------------------------------------------------

    def selected(self) -> LogMatch | None:
        rows = self.table.selectionModel().selectedRows()
        return self.matches[rows[0].row()] if rows else None

    def events_of(self, info: LogInfo) -> EventTable | str:
        if info.key not in self.events_cache:
            if info.task is None:
                self.events_cache[info.key] = "No task: no events to show."
            else:
                try:
                    self.events_cache[info.key] = info.task.extract(read_log_lines(info.path))
                except (OSError, ValueError, KeyError, IndexError) as e:
                    self.events_cache[info.key] = f"Can't extract events: {e!r}"
        return self.events_cache[info.key]

    def show_detail(self) -> None:
        m = self.selected()
        for button in (self.ok_button, self.error_button, self.use_button, self.clear_button):
            button.setEnabled(m is not None)
        self.events_table.setRowCount(0)
        self.events_summary.clear()
        self.fit_label.setText("")
        if m is None:
            self.detail_title.setText("Select a log above.")
            return
        info = m.info
        self.use_button.setEnabled(bool(same_day_rivals(self.matches, m)))
        self.note_edit.setText(info.correction.note)
        notes = "; ".join(filter(None, [m.hint, *info.notes])) or "no issues found"
        self.detail_title.setText(
            f"<b>{info.key}</b> -- {m.status}"
            f"{' -> ' + m.session.name if m.session else ''}, {info.state}: {notes}"
        )
        events = self.events_of(info)
        if isinstance(events, str):
            self.fit_label.setText(events)
            self.fit_label.setStyleSheet(f"color: {BAD_COLOR};")
            return
        run = None
        if m.session is not None and info.task is not None:
            run = bold_run_length(self.bids_root, info.subject, m.session.label, info.task.task)
        text, color = fit_text(events, run)
        self.fit_label.setText(text)
        self.fit_label.setStyleSheet(f"color: {color};")
        self.events_summary.setPlainText("\n".join(events_summary(events)))
        self.events_table.setColumnCount(4)
        self.events_table.setHorizontalHeaderLabels(
            ["onset (s)", "duration (s)", "trial_type", events.modulator]
        )
        rows = list(
            zip(
                events.onset,
                events.duration,
                events.trial_type,
                events.modulator_values,
                strict=True,
            )
        )
        self.events_table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                text = f"{value:.3f}" if isinstance(value, float) else str(value or "n/a")
                self.events_table.setItem(row, column, QTableWidgetItem(text))
        self.events_table.resizeColumnsToContents()

    def _set(self, key: str, review: str | None, note: str = "") -> None:
        if review:
            self.verdicts[key] = (review, note)
        else:
            self.verdicts.pop(key, None)

    def set_verdict(self, review: str | None) -> None:
        m = self.selected()
        if m is None:
            return
        self._set(m.info.key, review, self.note_edit.text().strip() if review else "")
        self.recheck()

    def use_this_one(self) -> None:
        m = self.selected()
        if m is None:
            return
        chosen = m.info
        self._set(chosen.key, REVIEW_OK, self.note_edit.text().strip())
        for rival in same_day_rivals(self.matches, m):
            if rival.info.correction.review == REVIEW_ERROR:
                continue  # already an error: keep that verdict and its note
            self._set(rival.info.key, REVIEW_NOT_USED, f"{chosen.path.name} used instead")
        self.recheck()

    def on_export(self) -> None:
        """Save as: pick a layout and a folder, then write every log's events.tsv there."""
        self.recheck()  # export what the table shows, with the corrections typed so far
        layouts = {
            "One folder (sub-..._events.tsv side by side)": ONE_FOLDER,
            "BIDS folders (sub-.../ses-.../func/)": BIDS_LAYOUT,
        }
        choice, ok = QInputDialog.getItem(
            self, "Export logs", "Lay the events files out as:", list(layouts), 0, False
        )
        if not ok:
            return
        chosen = QFileDialog.getExistingDirectory(
            self, "Export the events files to", str(self.bids_root.parent)
        )
        if not chosen:
            return
        out_root = Path(chosen)
        for raw, what in ((self.behav_root, "behavioural logs"), (self.dicom_root, "DICOMs")):
            if paths_conflict(out_root, raw):
                QMessageBox.warning(
                    self, "Pick another folder", f"Not in (or around) the raw {what}."
                )
                return
        if paths_conflict(out_root, self.bids_root):
            QMessageBox.warning(
                self,
                "Pick another folder",
                "Not in (or around) the BIDS folder: Convert to BIDS puts the events there.",
            )
            return
        self.export_button.setEnabled(False)
        self.progress_bar.setFormat("Exporting %v / %m logs")
        self.progress_bar.setVisible(True)

        def progress(done: int, total: int) -> None:
            self.progress_bar.setRange(0, max(total, 1))
            self.progress_bar.setValue(done)
            QApplication.processEvents()

        try:
            report = export_events(self.matches, out_root, layouts[choice], progress)
        finally:
            self.progress_bar.setVisible(False)
            self.progress_bar.setFormat("Re-checking %v / %m logs")
            self.export_button.setEnabled(True)
        lines = [f"{len(report.written)} events file(s) written to {out_root}."]
        if report.left_out:
            lines.append(f"{len(report.left_out)} left out (marked error / not used, or no task).")
        if report.failed:
            lines.append(f"{len(report.failed)} failed:")
            lines += [f"  {log}: {why}" for log, why in report.failed[:10]]
        unplaced = sum(UNPLACED_DIR in p.parts for p in report.written)
        if unplaced:
            lines.append(f"{unplaced} not matched to a session: in {UNPLACED_DIR}/.")
        box = QMessageBox.warning if report.failed else QMessageBox.information
        box(self, "Logs exported", "\n".join(lines))

    def save(self) -> None:
        corrections = self.corrections()
        dates, sessions = self.dicom_dates(), self.dicom_sessions()
        if corrections is None or dates is None or sessions is None:
            self.summary.setText(
                "Not saved: fix the cells marked red (dates as YYYY-MM-DD, sessions as a number)."
            )
            return
        # Keep corrections for logs not under this behaviour folder right now.
        keys = {info.key for info in self.infos}
        kept = {k: v for k, v in read_corrections(self.bids_root).items() if k not in keys}
        write_corrections(self.bids_root, kept | corrections)
        folders = set(self.session_edits)
        kept_ids = {
            k: v for k, v in read_dicom_corrections(self.bids_root).items() if k not in folders
        }
        write_dicom_corrections(self.bids_root, kept_ids | self.dicom_corrections())
        # The same for dates and session numbers: keep those of folders not listed now.
        kept_dates = {
            k: v for k, v in read_dicom_date_corrections(self.bids_root).items() if k not in folders
        }
        write_dicom_date_corrections(self.bids_root, kept_dates | dates)
        kept_sessions = {
            k: v
            for k, v in read_dicom_session_corrections(self.bids_root).items()
            if k not in folders
        }
        write_dicom_session_corrections(self.bids_root, kept_sessions | sessions)
        self.saved_dates, self.saved_sessions = dates, sessions
        self.summary.setText(self.summary.text() + "  (saved)")
        self.saved.emit()


class BehavLogsPage(QWidget):
    """The ID & Behaviour Logs tab. Reading every log and DICOM folder takes a while, so it
    happens on a worker thread with a progress bar: as soon as the toolbox opens (the tab is
    ready when you get to it), and again when a folder changes or on Reload."""

    badge_changed = Signal(str)

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
        self.filler = QWidget()  # keeps the row at the top until the logs are loaded
        layout.addWidget(self.filler, 1)
        self.layout_ = layout
        state.folder_changed.connect(self.on_folder_changed)
        self.update_status()

    def folders(self) -> tuple[Path, Path, Path] | None:
        roots = [self.state.folder(key) for key in ("bids", "behav", "dicom")]
        bids_root, behav_root, dicom_root = roots
        if bids_root is None or behav_root is None or dicom_root is None:
            return None
        return bids_root, behav_root, dicom_root

    def update_status(self, text: str = "") -> None:
        ready = self.folders() is not None
        self.load_button.setEnabled(ready and self.loader is None)
        if not ready:
            text = (
                "Choose the DICOM sessions, behavioural logs and BIDS folders on the "
                "Setup & Convert to BIDS tab."
            )
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def show_page(self) -> None:
        if self.panel is None and self.loader is None and self.folders() is not None:
            self.load()

    def on_folder_changed(self, key: str) -> None:
        if key in ("bids", "behav", "dicom"):
            self.drop_panel()
            if self.folders() is not None:
                self.load()  # in the background: ready by the time you open the tab
        self.update_status()

    def drop_panel(self) -> None:
        if self.panel is not None:
            self.layout_.removeWidget(self.panel)
            self.panel.deleteLater()
            self.panel = None
            self.filler.show()
            self.badge_changed.emit("")

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

    def on_loaded(self, result: BehavData | str) -> None:
        if self.loader is not None:
            self.loader.wait()
        self.loader = None
        self.progress_bar.setVisible(False)
        folders = self.folders()
        if isinstance(result, str) or folders is None:
            self.update_status(f"Can't read the logs: {result}" if isinstance(result, str) else "")
            return
        self.panel = BehavLogsPanel(*folders, self, data=result)
        self.panel.saved.connect(self.on_saved)
        if VIEW_ONLY in self.state.busy:  # corrections feed Convert to BIDS
            self.panel.save_button.setEnabled(False)
            self.panel.save_button.setToolTip("Locked: view-only review package.")
        self.filler.hide()
        self.layout_.addWidget(self.panel, 1)
        self.update_status()
        self.badge_changed.emit(self.badge())

    def wait(self) -> None:
        """Let a running read finish (closing the window, or a test)."""
        if self.loader is not None:
            self.loader.wait()
            QApplication.processEvents()  # delivers its `done`

    def badge(self) -> str:
        waiting = sum(needs_review(m) for m in self.panel.matches) if self.panel else 0
        return f"{waiting} to review" if waiting else ""

    def on_saved(self) -> None:
        self.badge_changed.emit(self.badge())
        QMessageBox.information(
            self, "Saved", "Corrections saved. Convert to BIDS (tab 1) to apply them."
        )
