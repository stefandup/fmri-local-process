"""The BIDS Crosscheck tab: review a converted Philani BIDS folder, session by session.

Modelled on vr_lab_toolbox's BIDS crosscheck tools: sessions on the left, the selected
session's files and problems on the right. All checks read the BIDS folder; nothing but the
human's own moves/deletes/trims/notes is written to it, and each of those is logged in its
code/crosscheck_actions.tsv.

Tick sessions (the box beside the ID) to act on several at once: delete them (dud visits)
or give them a note. While another job changes the BIDS folder (converting, trimming, C-PAC
reading it), the actions that change files are locked; notes stay open.

`fmri_crosscheck` opens the toolbox on this tab.
"""

from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.gui.folder_panel import FolderPanel
from fmri_local_process.gui.image_view import HelpToggle
from fmri_local_process.gui.qt_common import (
    WARNING_COLOR,
    NoticeBanner,
    bids_notices,
    primary_action_stylesheet,
    reveal,
)
from fmri_local_process.gui.toolbox_state import ToolboxState
from fmri_local_process.gui.trimming import Trimmer
from fmri_local_process.processing.crosscheck import (
    CrosscheckError,
    DataFile,
    SessionReview,
    delete_data_file,
    delete_session,
    keep_run,
    review_bids_folder,
    set_crosschecked,
    set_note,
    sidecar_note,
)

ICONS = {
    "pending": "⚠",
    "short": "❗",
    "long": "✂",
    "missing": "○",
    "check": "⚑",
    "extra": "⊕",
}
COLORS = {
    "pending": "#f39c12",
    "short": "#e74c3c",
    "long": "#3498db",
    "missing": "#9b59b6",
    "check": "#e67e22",
    "extra": "#16a085",
    "ok": "#2ecc71",
}
TABLE_COLUMNS = ("ID", "Date", "Session", "Files", "Problems", "Note")
PROBLEMS_COLUMN = 4
NOTE_COLUMN = 5
MAX_LISTED = 15
OK_ICON = "●"
CROSSCHECKED_ICON = "☑"  # vr_lab_toolbox's mark for "reviewed by a human"
CROSSCHECKED_COLOR = "#3498db"
# vr_lab_toolbox's "Mark crosschecked" button: filled green, the main thing to click.
MARK_COLOR = "#2ecc71"
MARK_HOVER_COLOR = "#27ae60"
MARK_BUTTON_HEIGHT = 34
LEGEND = (
    f"{CROSSCHECKED_ICON} crosschecked: marked as reviewed by you\n"
    f"{OK_ICON} nothing to resolve\n"
    f"{ICONS['pending']} a scan converted as several runs: keep one\n"
    f"{ICONS['short']} shorter than expected (or than the same scan elsewhere), or no events\n"
    f"{ICONS['long']} longer than expected: trim it\n"
    f"{ICONS['missing']} expected file missing\n"
    f"{ICONS['check']} events from a log dated other than the scan: check it's the right log\n"
    f"{ICONS['extra']} the subject has more sessions than expected: is one a dud to delete?"
)


HELP_TEXT = (
    "Click a session to see its files and problems on the right. Once you have been "
    "through it, <b>Mark crosschecked</b> (it then shows "
    f"{CROSSCHECKED_ICON}, whatever its problems). Tick sessions (the box beside the ID; "
    "<b>Select all</b> / <b>Select none</b>) to mark, note or delete several at once. "
    "Deleting a session leaves its DICOM folder out of conversion from now on; converting "
    "again renumbers the remaining sessions. Every change is logged (Overview tab, Action "
    "log) and kept in the crosscheck backup (tab 1).<br><br>"
    "<b>Problems</b>:<br>" + LEGEND.replace("\n", "<br>")
)


def problem_text(review: SessionReview) -> str:
    if review.crosschecked:
        return f"{CROSSCHECKED_ICON} crosschecked"
    counts = {kind: sum(i.kind == kind for i in review.issues) for kind in ICONS}
    parts = [f"{ICONS[kind]} {n}" for kind, n in counts.items() if n]
    return "   ".join(parts) or f"{OK_ICON} ok"


def needs_a_look(review: SessionReview) -> bool:
    """Problems, and not marked as crosschecked yet."""
    return bool(review.issues) and not review.crosschecked


def mark_button(text: str, tooltip: str) -> QPushButton:
    """vr_lab_toolbox's filled green 'Mark crosschecked' button."""
    button = QPushButton(text)
    button.setToolTip(tooltip)
    button.setMinimumHeight(MARK_BUTTON_HEIGHT)
    button.setStyleSheet(primary_action_stylesheet(MARK_COLOR, MARK_HOVER_COLOR))
    return button


def problem_color(review: SessionReview) -> str:
    if review.crosschecked:
        return CROSSCHECKED_COLOR
    kinds = {i.kind for i in review.issues}
    order = ("short", "pending", "check", "extra", "long", "missing")
    return next((COLORS[k] for k in order if k in kinds), COLORS["ok"])


class CrosscheckPage(QWidget):
    def __init__(self, state: ToolboxState, trimmer: Trimmer) -> None:
        super().__init__()
        self.state, self.trimmer = state, trimmer
        self.reviews: list[SessionReview] = []
        self.ticked: set[str] = set()  # names of the ticked sessions (sub-X_ses-N)
        self.seen_generation = -1
        self._build_ui()
        trimmer.line.connect(self.log_line)
        state.busy_changed.connect(self.on_busy_changed)

    # ---- layout -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.bids_panel = FolderPanel(
            self.state,
            "bids",
            "BIDS Folder",
            "The BIDS folder being crosschecked (chosen on the Setup & Convert to BIDS tab).",
            home_base=True,
            can_browse=False,
        )
        rescan = QPushButton("Rescan")
        rescan.setToolTip("Read the BIDS folder again (e.g. after changing files by hand).")
        rescan.clicked.connect(self.rescan)
        self.bids_panel.buttons.insertWidget(0, rescan)
        top.addWidget(self.bids_panel, 1)
        self.lock_label = QLabel()
        self.lock_label.setStyleSheet(f"color: {WARNING_COLOR};")
        top.addWidget(self.lock_label)
        layout.addLayout(top)
        self.banner = NoticeBanner()
        layout.addWidget(self.banner)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        tick_row = QHBoxLayout()
        self.help = HelpToggle("Help ?", "How to crosscheck, and what the icons mean.")
        self.help.set_text(HELP_TEXT)
        tick_row.addWidget(self.help.button)
        tick_row.addStretch(1)
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.setToolTip("Tick every session in the list.")
        self.select_all_button.clicked.connect(lambda: self.tick_all(True))
        tick_row.addWidget(self.select_all_button)
        self.select_none_button = QPushButton("Select none")
        self.select_none_button.clicked.connect(self.on_select_none)
        tick_row.addWidget(self.select_none_button)
        left_layout.addLayout(tick_row)
        left_layout.addWidget(self.help.box)
        self.table = QTableWidget(0, len(TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(TABLE_COLUMNS))
        problems_header = QTableWidgetItem("Problems")
        problems_header.setToolTip(LEGEND)
        self.table.setHorizontalHeaderItem(PROBLEMS_COLUMN, problems_header)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self.render_detail)
        self.table.itemChanged.connect(self.on_tick_changed)
        left_layout.addWidget(self.table, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self._build_ticked_group())
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        right_layout.addWidget(self.detail_scroll, 1)
        splitter.addWidget(right)
        splitter.setSizes([480, 820])
        layout.addWidget(splitter, 1)

        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.activity = QTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumHeight(120)
        layout.addWidget(self.activity)

    def _build_ticked_group(self) -> QGroupBox:
        """Actions on the ticked sessions; stays in place (buttons disabled when none)."""
        group = QGroupBox("Ticked sessions")
        layout = QHBoxLayout(group)
        self.ticked_label = QLabel()
        layout.addWidget(self.ticked_label)
        layout.addStretch(1)
        self.note_ticked_button = QPushButton("Note...")
        self.note_ticked_button.setToolTip("Give the ticked sessions a note (empty clears it).")
        self.note_ticked_button.clicked.connect(self.on_note_ticked)
        layout.addWidget(self.note_ticked_button)
        self.delete_ticked_button = QPushButton("Delete sessions...")
        self.delete_ticked_button.setToolTip(
            "Delete the ticked session folders (dud visits). Their DICOM folders are left out "
            "of conversion from now on, and the next conversion renumbers the remaining "
            "sessions."
        )
        self.delete_ticked_button.clicked.connect(self.on_delete_ticked)
        layout.addWidget(self.delete_ticked_button)
        self.unmark_ticked_button = QPushButton("Un-mark")
        self.unmark_ticked_button.setToolTip("Take the crosschecked mark off the ticked sessions.")
        self.unmark_ticked_button.clicked.connect(lambda: self.mark(self.ticked_reviews(), False))
        layout.addWidget(self.unmark_ticked_button)
        self.mark_ticked_button = mark_button(
            "Mark crosschecked", "Mark every ticked session as reviewed."
        )
        self.mark_ticked_button.clicked.connect(lambda: self.mark(self.ticked_reviews(), True))
        layout.addWidget(self.mark_ticked_button)
        return group

    # ---- state --------------------------------------------------------------------------

    def lock_reason(self) -> str:
        """Why the actions that change files are locked now ('' if they aren't)."""
        return self.state.lock_reason()

    def on_busy_changed(self) -> None:
        reason = self.lock_reason()
        self.lock_label.setText(f"Locked: {reason}." if reason else "")
        self.update_ticked()
        self.render_detail()

    def show_page(self) -> None:
        """Called when the tab is shown: rescan if the BIDS folder changed since."""
        if self.seen_generation != self.state.generation:
            self.rescan()

    def update_ticked(self) -> None:
        n = len(self.ticked_reviews())
        self.ticked_label.setText(
            f"{n} ticked" if n else "Tick sessions on the left to act on several at once."
        )
        self.note_ticked_button.setEnabled(n > 0)
        self.delete_ticked_button.setEnabled(n > 0 and not self.lock_reason())
        self.delete_ticked_button.setText(
            f"Delete {n} session(s)..." if n else "Delete sessions..."
        )
        ticked = self.ticked_reviews()
        self.mark_ticked_button.setEnabled(any(not r.crosschecked for r in ticked))
        self.mark_ticked_button.setText(f"Mark {n} crosschecked" if n else "Mark crosschecked")
        self.unmark_ticked_button.setEnabled(any(r.crosschecked for r in ticked))
        self.select_none_button.setEnabled(n > 0)
        self.select_all_button.setEnabled(n < len(self.reviews))

    def badge(self) -> str:
        waiting = sum(needs_a_look(r) for r in self.reviews)
        return f"{waiting} to check" if waiting else ""

    # ---- scanning ---------------------------------------------------------------------------

    def rescan(self) -> None:
        self.seen_generation = self.state.generation
        bids_root = self.state.folder("bids")
        selected = self.selected_review()
        self.reviews = review_bids_folder(bids_root) if bids_root else []
        self.banner.set_messages(bids_notices(bids_root, bool(self.reviews)))
        self.ticked &= {r.name for r in self.reviews}
        self.table.blockSignals(True)  # no itemChanged (tick) signals while filling
        self.table.setRowCount(len(self.reviews))
        for row, review in enumerate(self.reviews):
            cells = (
                review.subject,
                review.date or "?",
                f"ses-{review.session}" if review.session else "-",
                str(len(review.files) + len(review.pending)),
                problem_text(review),
                review.note,
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    ticked = review.name in self.ticked
                    item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
                if column == 1 and review.source_folder:
                    item.setToolTip(f"Converted from {review.source_folder}")
                if column == PROBLEMS_COLUMN:
                    item.setForeground(QColor(problem_color(review)))
                    item.setToolTip(LEGEND)
                if column == NOTE_COLUMN:
                    item.setToolTip(review.note)
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self.update_ticked()
        waiting = sum(needs_a_look(r) for r in self.reviews)
        marked = sum(r.crosschecked for r in self.reviews)
        self.summary.setText(
            f"{len(self.reviews)} session(s): {marked} crosschecked, {waiting} still to check"
            if bids_root
            else "Choose the BIDS folder on the Setup & Convert to BIDS tab."
        )
        names = [r.name for r in self.reviews]
        if selected and selected.name in names:
            self.table.selectRow(names.index(selected.name))
        elif self.reviews:
            self.table.selectRow(0)
        else:
            self.render_detail()

    def selected_review(self) -> SessionReview | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.reviews[rows[0].row()] if rows and rows[0].row() < len(self.reviews) else None

    def ticked_reviews(self) -> list[SessionReview]:
        return [r for r in self.reviews if r.name in self.ticked]

    def on_tick_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or item.row() >= len(self.reviews):
            return
        name = self.reviews[item.row()].name
        if item.checkState() == Qt.CheckState.Checked:
            self.ticked.add(name)
        else:
            self.ticked.discard(name)
        self.update_ticked()

    def on_select_none(self) -> None:
        self.tick_all(False)

    def tick_all(self, ticked: bool) -> None:
        self.ticked = {r.name for r in self.reviews} if ticked else set()
        state = Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(state)
        self.table.blockSignals(False)
        self.update_ticked()

    def mark(self, reviews: list[SessionReview], marked: bool) -> None:
        bids_root = self.state.folder("bids")
        if bids_root is None or not reviews:
            return
        names = ", ".join(r.name for r in reviews[:MAX_LISTED])
        what = "Marked crosschecked" if marked else "Un-marked"
        self.run_action(f"{what}: {names}", partial(set_crosschecked, bids_root, reviews, marked))

    # ---- detail pane -------------------------------------------------------------------------

    def render_detail(self) -> None:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        review = self.selected_review()
        bids_root = self.state.folder("bids")
        if review is None or bids_root is None:
            layout.addWidget(QLabel("Select a session."))
        else:
            scanned = f"scanned {review.date}" if review.date else "scan date unknown"
            source = f" from {review.source_folder}" if review.source_folder else ""
            title_row = QHBoxLayout()
            title_row.addWidget(QLabel(f"<h2>{review.name}</h2>{scanned}{source}"), 1)
            title_row.addWidget(self.crosscheck_widget(review))
            layout.addLayout(title_row)
            layout.addLayout(self.session_row(review, bids_root))
            if review.issues:
                layout.addWidget(self.issues_group(review))
            for label in sorted({d.label for d in review.pending}):
                candidates = [d for d in review.pending if d.label == label]
                layout.addWidget(self.pick_run_group(label, candidates, bids_root))
            if review.files:
                layout.addWidget(self.files_group(review, bids_root))
        layout.addStretch(1)
        self.detail_scroll.setWidget(widget)

    def crosscheck_widget(self, review: SessionReview) -> QWidget:
        """'Mark crosschecked' (filled green), or '☑ Crosschecked' with a plain Un-mark."""
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        if review.crosschecked:
            label = QLabel(
                f'<span style="color:{CROSSCHECKED_COLOR}">{CROSSCHECKED_ICON} Crosschecked</span>'
            )
            label.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(label)
            unmark = QPushButton("Un-mark crosschecked")
            unmark.setToolTip("Take the reviewed mark off this session.")
            unmark.clicked.connect(lambda: self.mark([review], False))
            row.addWidget(unmark)
        else:
            button = mark_button(
                "Mark crosschecked",
                "You have been through this session: mark it as reviewed, whatever its "
                "problems (e.g. a short scan that is as good as it gets).",
            )
            button.setEnabled(bool(review.source_folder))
            if not review.source_folder:
                button.setToolTip("Convert to BIDS first: its DICOM folder isn't recorded.")
            button.clicked.connect(lambda: self.mark([review], True))
            row.addWidget(button)
        return widget

    def session_row(self, review: SessionReview, bids_root: Path) -> QHBoxLayout:
        """Note box and 'delete this session' for the session shown."""
        row = QHBoxLayout()
        row.addWidget(QLabel("Note:"))
        note = QLineEdit(review.note)
        note.setPlaceholderText("e.g. scan stopped early, participant moved a lot...")
        row.addWidget(note, 1)
        save = QPushButton("Save note")
        save.clicked.connect(lambda: self.save_note([review], note.text()))
        note.returnPressed.connect(save.click)
        row.addWidget(save)
        delete = QPushButton("Delete session...")
        delete.setToolTip(
            "Delete this session folder (a dud visit). Its DICOM folder is left out of "
            "conversion from now on; the next conversion renumbers the remaining sessions."
        )
        delete.setEnabled(review.session is not None and not self.lock_reason())
        delete.clicked.connect(lambda: self.delete_sessions([review], bids_root))
        row.addWidget(delete)
        return row

    def issues_group(self, review: SessionReview) -> QGroupBox:
        group = QGroupBox("Problems")
        layout = QVBoxLayout(group)
        for issue in review.issues:
            label = QLabel(
                f'<span style="color:{COLORS[issue.kind]}">{ICONS[issue.kind]}</span> {issue.text}'
            )
            label.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(label)
        return group

    def files_group(self, review: SessionReview, bids_root: Path) -> QGroupBox:
        group = QGroupBox(f"Files ({len(review.files)})")
        grid = QGridLayout(group)
        flagged = {i.path: i for i in review.issues if i.kind in ("short", "long")}
        unlocked = not self.lock_reason()
        for row, data in enumerate(review.files):
            issue = flagged.get(data.path)
            kind = issue.kind if issue else "ok"
            mark = QLabel(ICONS[kind] if issue else "✓")
            mark.setStyleSheet(f"color: {COLORS[kind]};")
            grid.addWidget(mark, row, 0)
            grid.addWidget(QLabel(f"{data.datatype}/{data.path.name}"), row, 1)
            grid.addWidget(QLabel(data.summary()), row, 2)
            if issue and issue.trim_to:
                trim = QPushButton(f"Trim to {issue.trim_to}...")
                trim.setEnabled(unlocked)
                trim.clicked.connect(partial(self.trimmer.start, self, [(data, issue.trim_to)]))
                grid.addWidget(trim, row, 5)
            reveal_button = QPushButton("Reveal")
            reveal_button.clicked.connect(partial(reveal, data.path.parent))
            grid.addWidget(reveal_button, row, 3)
            delete = QPushButton("Delete...")
            delete.setToolTip(
                "Delete this file and its sidecar. Convert to BIDS recreates it from the raw "
                "data, e.g. once an incomplete copy has been redone."
            )
            delete.setEnabled(unlocked)
            delete.clicked.connect(partial(self.on_delete, data, bids_root))
            grid.addWidget(delete, row, 4)
        grid.setColumnStretch(1, 1)
        return group

    def pick_run_group(self, label: str, candidates: list[DataFile], bids_root: Path) -> QGroupBox:
        group = QGroupBox(f"Keep one: {label} ({len(candidates)} runs in this session)")
        group.setStyleSheet(f"QGroupBox {{ color: {COLORS['pending']}; }}")
        layout = QVBoxLayout(group)
        buttons = QButtonGroup(group)
        for index, data in enumerate(candidates):
            note = sidecar_note(data)
            radio = QRadioButton(f"{data.path.name}   {data.summary()}   {note}")
            buttons.addButton(radio, index)
            layout.addWidget(radio)
        row = QHBoxLayout()
        keep = QPushButton("Keep selected run, delete the others")
        keep.setEnabled(not self.lock_reason())
        keep.clicked.connect(lambda: self.on_keep_run(buttons, candidates, bids_root))
        row.addWidget(keep)
        reveal_button = QPushButton("Reveal")
        reveal_button.clicked.connect(partial(reveal, candidates[0].path.parent))
        row.addWidget(reveal_button)
        row.addStretch(1)
        layout.addLayout(row)
        return group

    # ---- actions -----------------------------------------------------------------------------

    def confirm(self, text: str) -> bool:
        answer = QMessageBox.question(self, "Confirm", text)
        return answer == QMessageBox.StandardButton.Yes

    def run_action(self, description: str, action: partial) -> None:
        try:
            action()
        except (CrosscheckError, OSError) as e:
            QMessageBox.warning(self, "Not done", str(e))
            self.log_line(f"ERROR  {description}: {e}")
            return
        self.log_line(description)
        self.state.bump()
        self.rescan()

    def on_keep_run(
        self, buttons: QButtonGroup, candidates: list[DataFile], bids_root: Path
    ) -> None:
        index = buttons.checkedId()
        if index < 0:
            QMessageBox.information(self, "Pick a run", "Select the run to keep first.")
            return
        chosen = candidates[index]
        others = len(candidates) - 1
        if self.confirm(f"Keep {chosen.path.name} and delete the {others} other run(s)?"):
            self.run_action(
                f"Kept {chosen.path.name}", partial(keep_run, bids_root, chosen, candidates)
            )

    def on_delete(self, data: DataFile, bids_root: Path) -> None:
        if self.confirm(f"Delete {data.path.name} and its sidecar from the BIDS folder?"):
            self.run_action(f"Deleted {data.path.name}", partial(delete_data_file, bids_root, data))

    def save_note(self, reviews: list[SessionReview], text: str) -> None:
        bids_root = self.state.folder("bids")
        if bids_root is None:
            return
        names = ", ".join(r.name for r in reviews)
        self.run_action(f"Note for {names}: {text.strip() or '(cleared)'}",
                        partial(set_note, bids_root, reviews, text))  # fmt: skip

    def on_note_ticked(self) -> None:
        reviews = self.ticked_reviews()
        if not reviews:
            return
        notes = {r.note for r in reviews}
        text, ok = QInputDialog.getText(
            self,
            "Note",
            f"Note for {len(reviews)} ticked session(s) (empty clears it):",
            text=notes.pop() if len(notes) == 1 else "",
        )
        if ok:
            self.save_note(reviews, text)

    def on_delete_ticked(self) -> None:
        bids_root = self.state.folder("bids")
        if bids_root is not None:
            self.delete_sessions(self.ticked_reviews(), bids_root)

    def delete_sessions(self, reviews: list[SessionReview], bids_root: Path) -> None:
        reviews = [r for r in reviews if r.session is not None]
        if not reviews:
            return
        if reason := self.lock_reason():
            QMessageBox.information(self, "Not now", f"Can't delete now: {reason}.")
            return
        listing = "\n".join(
            f"  {r.name}  scanned {r.date or '?'}  from {r.source_folder or '?'}"
            + (f"  ({len(r.issues)} problem(s))" if r.issues else "")
            for r in reviews[:MAX_LISTED]
        )
        more = f"\n  ... and {len(reviews) - MAX_LISTED} more" if len(reviews) > MAX_LISTED else ""
        if not self.confirm(
            f"Delete {len(reviews)} session folder(s) from the BIDS folder?\n{listing}{more}\n\n"
            "Their DICOM folders are left out of conversion from now on (listed in "
            "code/excluded_sessions.json; remove a line there to convert it again). Convert "
            "to BIDS afterwards to renumber the remaining sessions."
        ):
            return
        for review in reviews:
            try:
                delete_session(bids_root, review, review.note)
            except (CrosscheckError, OSError) as e:
                QMessageBox.warning(self, "Not deleted", str(e))
                self.log_line(f"ERROR  deleting {review.name}: {e}")
                continue
            self.ticked.discard(review.name)
            self.log_line(f"Deleted {review.name} (from {review.source_folder})")
        self.log_line("Convert to BIDS to renumber the remaining sessions.")
        self.state.bump()
        self.rescan()

    def log_line(self, text: str) -> None:
        self.activity.append(text)


def main() -> None:
    """The toolbox (with its splash), opened on the BIDS Crosscheck tab."""
    from fmri_local_process.gui.launcher import crosscheck

    crosscheck()


if __name__ == "__main__":
    main()
