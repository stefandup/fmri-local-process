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
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
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
    read_dicom_corrections,
    read_excluded_sessions,
    scan_dicom_root,
    session_infos,
    write_dicom_corrections,
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
    "DICOM folder", "Scanned", "ID in folder", "Corrected ID", "Resolves to",
    "BIDS session", "Tasks", "Logs", "Notes",
)  # fmt: skip
S_CORRECTED, S_RESOLVED, S_SESSION, S_TASKS, S_LOGS, S_NOTES = range(3, 9)

Verdict = tuple[str, str]  # (review, note)


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


class BehavLogsDialog(QDialog):
    def __init__(self, bids_root: Path, behav_root: Path, dicom_root: Path, parent: QWidget | None):
        super().__init__(parent)
        self.setWindowTitle("IDs & behaviour logs")
        self.resize(1500, 900)
        self.bids_root, self.behav_root = bids_root, behav_root
        self.events_cache: dict[str, EventTable | str] = {}  # key -> table, or why it failed
        self.behav_tasks = {t.task for t in BEHAV_TASKS}  # tasks that should have a log

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.infos = inspect_logs(behav_root, read_corrections(bids_root))
            self.scanned = scan_dicom_root(dicom_root)
            dicom_corrections = read_dicom_corrections(bids_root)
            self.excluded = set(read_excluded_sessions(bids_root))  # deleted at crosscheck
            self.sessions: list[SessionInfo] = session_infos(
                self.scanned, dicom_corrections, self.excluded
            )
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.verdicts: dict[str, Verdict] = {
            info.key: (info.correction.review, info.correction.note)
            for info in self.infos
            if info.correction.review
        }

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Each log is matched to a scan session by subject ID and date. A twin's 12345-1 "
            "becomes 12345T1; other characters (e.g. a space) are removed and flagged. Correct "
            "a wrong ID or date (Behaviour logs tab) or a wrong subject ID in a DICOM folder "
            "name (Scan sessions tab: scan date and time shown, to tell twins apart) and press "
            "Re-check. Select a log to see its events and mark it OK or as an error; of several "
            "logs on one day, pick the one to use. Save keeps all of this, and Convert / update "
            "BIDS applies it. Raw files are never renamed. Sessions already converted under an "
            "old ID stay in the BIDS folder until you remove them.\n"
            f"Saved in {bids_root / CORRECTIONS_FILE} and {bids_root / DICOM_CORRECTIONS_FILE}."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        controls = QHBoxLayout()
        self.problems_only = QCheckBox("Needs review only")
        self.problems_only.setToolTip("Hide logs that are fine or already have a verdict.")
        self.problems_only.toggled.connect(self.apply_filter)
        controls.addWidget(self.problems_only)
        recheck = QPushButton("Re-check")
        recheck.setToolTip("Match again with the corrections typed so far (not saved yet).")
        recheck.clicked.connect(self.recheck)
        controls.addWidget(recheck)
        controls.addStretch(1)
        self.summary = QLabel()
        controls.addWidget(self.summary)
        layout.addLayout(controls)

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

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
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

    def recheck(self) -> None:
        corrections = self.corrections()
        if corrections is None:
            self.summary.setText("Fix the dates marked red (YYYY-MM-DD).")
            return
        for info in self.infos:
            info.correction = corrections.get(info.key, Correction())
        self.sessions = session_infos(self.scanned, self.dicom_corrections(), self.excluded)
        self.show_matches(match_logs(self.infos, self.sessions))
        self.show_detail()

    def show_matches(self, matches: list[LogMatch]) -> None:
        self.matches = matches
        for row, m in enumerate(matches):
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
                notes.append("not converted: no date or ID in the folder name")
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

    def save(self) -> None:
        corrections = self.corrections()
        if corrections is None:
            self.summary.setText("Not saved: fix the dates marked red (YYYY-MM-DD).")
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
        self.accept()
