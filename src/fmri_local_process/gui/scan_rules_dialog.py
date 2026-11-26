"""Scan rules: which DICOM series folder becomes which BIDS scan.

Opened from the Setup & Convert to BIDS tab. Each rule is a folder pattern and the BIDS
scan it makes; several rules can make the same scan (e.g. the Philani T1 and a clinical
t1_mprage both make T1w; a session with both gets them as runs to pick from). The rules are
saved with the BIDS folder (code/scan_rules.json), so they travel with the data and are
backed up; until they are changed the built-in Philani rules apply.

"Check DICOM folder" shows, per session folder, what the rules find and which series folders
nothing matches -- from the folder names only.
"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.app.scan_rules import HELP_TEXT, check_text
from fmri_local_process.gui.image_view import HelpToggle
from fmri_local_process.processing.philani_bids import (
    SCAN_RULES,
    SCAN_RULES_FILE,
    ScanRule,
    read_scan_rules,
    rule_from,
    write_scan_rules,
)

BAD_COLOR = "#e74c3c"
COLUMNS = ("Folder pattern", "BIDS scan")
PATTERN, SCAN = 0, 1


class ScanRulesDialog(QDialog):
    def __init__(self, bids_root: Path, dicom_root: Path | None, parent: QWidget | None):
        super().__init__(parent)
        self.setWindowTitle("Scan rules")
        self.resize(1000, 640)
        self.bids_root, self.dicom_root = bids_root, dicom_root
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        for text, action in (
            ("Add rule", lambda: self.add_row()),  # not clicked's `checked` as the pattern
            ("Remove selected", self.remove_selected),
            ("Reset to Philani defaults", self.reset),
        ):
            button = QPushButton(text)
            button.clicked.connect(action)
            top.addWidget(button)
        self.check_button = QPushButton("Check DICOM folder")
        self.check_button.setToolTip(
            "Show, per session folder, what these rules find and which series folders "
            "nothing matches (folder names only; nothing is converted)."
        )
        self.check_button.setEnabled(dicom_root is not None)
        self.check_button.clicked.connect(self.check)
        top.addWidget(self.check_button)
        top.addStretch(1)
        self.help = HelpToggle("Help ?", "How patterns and BIDS scans work.")
        self.help.set_text(HELP_TEXT + f"<br><br>Saved in {bids_root / SCAN_RULES_FILE}.")
        top.addWidget(self.help.button)
        layout.addLayout(top)
        layout.addWidget(self.help.box)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        splitter.addWidget(self.table)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setFont(QFont("Courier New"))
        self.report.setPlaceholderText("Check DICOM folder shows what these rules find here.")
        splitter.addWidget(self.report)
        splitter.setSizes([360, 220])
        layout.addWidget(splitter, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.fill(read_scan_rules(bids_root))

    def fill(self, rules: tuple[ScanRule, ...]) -> None:
        self.table.setRowCount(0)
        for rule in rules:
            self.add_row(rule.pattern, rule.scan)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    def add_row(self, pattern: str = "", scan: str = "") -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        pattern_item = QTableWidgetItem(pattern or "")
        self.table.setItem(row, PATTERN, pattern_item)
        self.table.setItem(row, SCAN, QTableWidgetItem(scan or ""))
        if not pattern:  # a new, empty row: start typing its pattern
            self.table.setCurrentCell(row, PATTERN)
            self.table.editItem(pattern_item)

    def remove_selected(self) -> None:
        for index in sorted(self.table.selectionModel().selectedRows(), reverse=True):
            self.table.removeRow(index.row())

    def reset(self) -> None:
        self.fill(SCAN_RULES)

    def rules(self) -> tuple[ScanRule, ...] | None:
        """The rules typed; None (bad rows marked red, reasons in tooltips) if any is wrong.
        Empty rows are ignored."""
        rules, ok = [], True
        for row in range(self.table.rowCount()):
            items = [self.table.item(row, c) for c in (PATTERN, SCAN)]
            pattern, scan = (i.text() if i is not None else "" for i in items)
            if not pattern.strip() and not scan.strip():
                continue
            try:
                rules.append(rule_from(pattern, scan))
                why, color = "", None
            except ValueError as e:
                why, color, ok = str(e), QColor(BAD_COLOR), False
            for item in items:
                if item is not None:
                    item.setBackground(color if color is not None else QColor(0, 0, 0, 0))
                    item.setToolTip(why)
        return tuple(rules) if ok else None

    def check(self) -> None:
        rules = self.rules()
        if rules is None or self.dicom_root is None:
            self.report.setPlainText("Fix the rows marked red first (hover them for why).")
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.report.setPlainText(check_text(self.dicom_root, rules))
        except OSError as e:
            self.report.setPlainText(f"Can't read the DICOM folder: {e}")
        finally:
            QGuiApplication.restoreOverrideCursor()

    def save(self) -> None:
        rules = self.rules()
        if rules is None:
            QMessageBox.warning(self, "Not saved", "Fix the rows marked red (hover them for why).")
            return
        if not rules:
            QMessageBox.warning(self, "Not saved", "Keep at least one rule.")
            return
        try:
            write_scan_rules(self.bids_root, rules)
        except OSError as e:
            QMessageBox.warning(self, "Not saved", str(e))
            return
        self.accept()
