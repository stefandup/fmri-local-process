"""The Overview tab, after the old Streamlit BIDS viewer (philani_scripts'
philaniBIDSspreadsheet.py): counts for the whole BIDS folder, then one table to scroll
through -- a row per session, per scan how many runs and how long -- with the cells that need
a look coloured (red: missing, yellow: short, orange: several runs or not defaced, blue: too
long; green: trimmed to the expected length, done), and each session's problems written out
in a few words. Export CSV writes the same table. A second tab lists the action log: every
change made to the BIDS folder, newest first, with a search box."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.app.crosscheck import CROSSCHECKED_COLOR, needs_a_look
from fmri_local_process.app.notices import bids_notices
from fmri_local_process.app.overview import (
    CELL_COLORS,
    CELL_LEGEND,
    PROBLEMS_LEGEND,
    SUMMARY_MAX_LINES,
    matches_search,
    problem_summary,
    summary_lines,
)
from fmri_local_process.gui.qt_common import NoticeBanner, wrap_tooltip
from fmri_local_process.gui.toolbox_state import ToolboxState
from fmri_local_process.processing.action_log import COLUMNS, read_actions
from fmri_local_process.processing.crosscheck import (
    SessionReview,
    overview_columns,
    overview_row,
    overview_tasks,
    read_expected_sessions,
    review_bids_folder,
    set_note,
    write_overview_csv,
)
from fmri_local_process.processing.philani_bids import read_excluded_sessions


class OverviewPage(QWidget):
    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        self.seen_generation = -1
        self.reviews: list[SessionReview] = []
        self.logged: list[dict[str, str]] = []
        layout = QVBoxLayout(self)
        self.banner = NoticeBanner()
        layout.addWidget(self.banner)

        summary = QGroupBox("BIDS folder summary")
        summary_layout = QVBoxLayout(summary)
        self.summary_label = QPlainTextEdit()  # a log, in a typewriter font
        self.summary_label.setReadOnly(True)
        self.summary_label.setFont(QFont("Courier New"))
        self.summary_label.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        summary_layout.addWidget(self.summary_label)
        layout.addWidget(summary)

        tabs = QTabWidget()
        tabs.addTab(self._build_sessions_tab(), "Sessions")
        tabs.addTab(self._build_history_tab(), "Action log")
        layout.addWidget(tabs, 1)
        self.refresh()

    def _build_sessions_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        self.problems_only = QCheckBox("Only sessions with something to check")
        self.problems_only.toggled.connect(self.apply_session_filter)
        row.addWidget(self.problems_only)
        legend = QLabel("Colours ⓘ")
        legend.setToolTip(CELL_LEGEND)
        row.addWidget(legend)
        row.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        self.export_button = QPushButton("Export CSV...")
        self.export_button.setToolTip(
            wrap_tooltip(
                "Save this table as a CSV (one row per session), with each session's DICOM "
                "folder, problems and note: for QC meetings or handing over."
            )
        )
        self.export_button.clicked.connect(self.on_export_csv)
        row.addWidget(self.export_button)
        layout.addLayout(row)
        self.session_table = QTableWidget(0, 0)
        self.session_table.verticalHeader().setVisible(False)
        # Only the Note column is editable (double-click it, or select it and press F2).
        self.session_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.session_table.itemChanged.connect(self.on_note_edited)
        self.session_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.session_table.setAlternatingRowColors(True)
        layout.addWidget(self.session_table, 1)
        return page

    def _build_history_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search: subject, action, file, user...")
        self.search.textChanged.connect(self.apply_filter)
        layout.addWidget(self.search)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([c.capitalize() for c in COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        return page

    def show_page(self) -> None:
        if self.seen_generation != self.state.generation:
            self.refresh()

    def refresh(self) -> None:
        self.seen_generation = self.state.generation
        bids_root = self.state.folder("bids")
        if bids_root is None:
            self.summary_label.setPlainText("Choose the BIDS folder on the Setup & Convert tab.")
            self.reviews, self.logged = [], []
        else:
            self.reviews = [r for r in review_bids_folder(bids_root) if r.session is not None]
            lines = summary_lines(
                self.reviews,
                read_expected_sessions(bids_root),
                len(read_excluded_sessions(bids_root)),
            )
            self.summary_label.setPlainText("\n".join(lines))
            self.logged = list(reversed(read_actions(bids_root)))
        self.banner.set_messages(bids_notices(bids_root, bool(self.reviews)))
        self.fit_summary()
        self.fill_sessions()
        self.fill_history()
        # Text set while the tab was hidden: size the summary box to it now.
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()

    def fit_summary(self) -> None:
        """As tall as its lines (up to SUMMARY_MAX_LINES; more scroll)."""
        lines = min(self.summary_label.document().blockCount(), SUMMARY_MAX_LINES)
        line_height = self.summary_label.fontMetrics().lineSpacing()
        margins = self.summary_label.contentsMargins()
        frame = 2 * self.summary_label.frameWidth() + margins.top() + margins.bottom()
        padding = 2 * int(self.summary_label.document().documentMargin())
        # One line of slack, so the last line never needs a scroll bar.
        self.summary_label.setFixedHeight((lines + 1) * line_height + frame + padding)

    def fill_sessions(self) -> None:
        tasks = overview_tasks(self.reviews)
        columns = [*overview_columns(tasks), "Problems", "Note"]
        table = self.session_table
        table.blockSignals(True)  # no itemChanged (note edits) while filling
        table.setSortingEnabled(False)  # filling a sorted table scrambles the rows
        table.clear()
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(columns)
        problems_header = QTableWidgetItem("Problems")
        problems_header.setToolTip(PROBLEMS_LEGEND)
        table.setHorizontalHeaderItem(len(columns) - 2, problems_header)
        note_header = QTableWidgetItem("Note")
        note_header.setToolTip("Double-click a note to write or change it (Enter saves).")
        table.setHorizontalHeaderItem(len(columns) - 1, note_header)
        table.setRowCount(len(self.reviews))
        black = QColor("black")
        for row, review in enumerate(self.reviews):
            for column, cell in enumerate(overview_row(review, tasks)):
                item = QTableWidgetItem(cell.text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if cell.status:
                    item.setBackground(QColor(CELL_COLORS[cell.status]))
                    item.setForeground(black)
                table.setItem(row, column, item)
            problems = QTableWidgetItem(problem_summary(review))
            problems.setFlags(problems.flags() & ~Qt.ItemFlag.ItemIsEditable)
            problems.setData(Qt.ItemDataRole.UserRole, needs_a_look(review))  # for the filter
            problems.setToolTip("\n".join(i.text for i in review.issues) or "Nothing to resolve")
            if needs_a_look(review):
                problems.setBackground(QColor(CELL_COLORS["missing"]))
                problems.setForeground(black)
            elif review.crosschecked:
                problems.setForeground(QColor(CROSSCHECKED_COLOR))
            table.setItem(row, len(columns) - 2, problems)
            note = QTableWidgetItem(review.note)
            note.setToolTip(review.note or "Double-click to write a note.")
            note.setData(Qt.ItemDataRole.UserRole, review.name)  # which session, when sorted
            table.setItem(row, len(columns) - 1, note)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        table.setSortingEnabled(True)
        table.blockSignals(False)
        self.export_button.setEnabled(bool(self.reviews))
        self.apply_session_filter()

    def apply_session_filter(self) -> None:
        only = self.problems_only.isChecked()
        problems_column = self.session_table.columnCount() - 2
        for row in range(self.session_table.rowCount()):
            item = self.session_table.item(row, problems_column)
            clean = item is not None and not item.data(Qt.ItemDataRole.UserRole)
            self.session_table.setRowHidden(row, only and clean)

    def on_note_edited(self, item: QTableWidgetItem) -> None:
        """A note typed into the table: save it as the BIDS Crosscheck tab would."""
        if item.column() != self.session_table.columnCount() - 1:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        review = next((r for r in self.reviews if r.name == name), None)
        bids_root = self.state.folder("bids")
        if review is None or bids_root is None or item.text().strip() == review.note:
            return
        try:
            set_note(bids_root, [review], item.text())
        except OSError as e:
            QMessageBox.warning(self, "Note not saved", str(e))
            return
        review.note = item.text().strip()
        item.setToolTip(review.note or "Double-click to write a note.")
        # The other tabs show notes too; this table already has it, so don't rebuild it.
        self.seen_generation = self.state.generation + 1
        self.state.bump()
        self.fill_history()

    def fill_history(self) -> None:
        self.table.setRowCount(len(self.logged))
        for row, action in enumerate(self.logged):
            for column, key in enumerate(COLUMNS):
                text = action.get(key, "") or ""
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.apply_filter()

    def apply_filter(self) -> None:
        for row, action in enumerate(self.logged):
            self.table.setRowHidden(row, not matches_search(action, self.search.text()))

    def on_export_csv(self) -> None:
        bids_root = self.state.folder("bids")
        if bids_root is None or not self.reviews:
            return
        start = str(bids_root.parent / f"{bids_root.name}_overview.csv")
        chosen, _ = QFileDialog.getSaveFileName(self, "Export CSV", start, "CSV files (*.csv)")
        if not chosen:
            return
        try:
            write_overview_csv(self.reviews, Path(chosen))
        except OSError as e:
            QMessageBox.warning(self, "Not exported", str(e))
            return
        QMessageBox.information(
            self, "Exported", f"{len(self.reviews)} session(s) saved to {chosen}"
        )
