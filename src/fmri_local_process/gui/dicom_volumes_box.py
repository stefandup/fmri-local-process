"""The 'Missing volumes' box on the Scan lengths tab: each BOLD run's DICOM series counted the
way dcm2niix counts it, next to the NIfTI's volumes (see processing/dicom_volumes.py)."""

from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from fmri_local_process.app.colors import ERROR_COLOR
from fmri_local_process.app.dicom_volumes import (
    COLUMNS,
    HELP_TEXT,
    PROBLEMS,
    RESULT_COLOR,
    CheckJob,
    check_cells,
    status_text,
)
from fmri_local_process.gui.image_view import HelpToggle
from fmri_local_process.gui.qt_common import style_secondary_label, wrap_tooltip
from fmri_local_process.processing import dicom_volumes as dv
from fmri_local_process.processing.crosscheck import DataFile


class CheckThread(QThread):
    progress = Signal(int, int)
    done = Signal(list, str)  # checks, error

    def __init__(self, bids_root: Path, dicom_root: Path, runs: list[DataFile]) -> None:
        super().__init__()
        self.job = CheckJob(bids_root, dicom_root, runs, self.progress.emit)

    def run(self) -> None:
        self.done.emit(*self.job.run())


class DicomVolumesBox(QGroupBox):
    def __init__(self) -> None:
        super().__init__("Missing volumes: DICOM images vs NIfTI")
        self.bids_root: Path | None = None
        self.dicom_root: Path | None = None
        self.runs: list[DataFile] = []
        self.checks: list[dv.VolumeCheck] = []
        self.worker: CheckThread | None = None
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.check_button = QPushButton("Check DICOMs for missing volumes")
        self.check_button.setToolTip(
            wrap_tooltip(
                "Count each BOLD run's DICOM images as dcm2niix does, and compare with the "
                "protocol and the NIfTI. Reads only header tags, never the patient fields."
            )
        )
        self.check_button.clicked.connect(self.start)
        row.addWidget(self.check_button)
        self.problems_only = QCheckBox("Only runs with problems")
        self.problems_only.toggled.connect(self.fill_table)
        row.addWidget(self.problems_only)
        self.status = QLabel()
        style_secondary_label(self.status)
        row.addWidget(self.status, 1)
        self.help = HelpToggle("Help ?", "What the DICOM check counts, and what it means.")
        self.help.set_text(HELP_TEXT)
        row.addWidget(self.help.button)
        layout.addLayout(row)
        layout.addWidget(self.help.box)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([name for name, _ in COLUMNS])
        for column, (_name, tip) in enumerate(COLUMNS):
            header = self.table.horizontalHeaderItem(column)
            if header is not None:
                header.setToolTip(wrap_tooltip(tip))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.set_folders(None, None, [])

    def set_folders(
        self, bids_root: Path | None, dicom_root: Path | None, data_files: list[DataFile]
    ) -> None:
        """New BIDS contents: the old results no longer hold."""
        self.bids_root, self.dicom_root = bids_root, dicom_root
        self.runs = dv.bold_runs(data_files)
        self.checks = []
        self.fill_table()
        self.update_controls()

    def update_controls(self) -> None:
        running = self.worker is not None
        ready = self.bids_root is not None and self.dicom_root is not None and bool(self.runs)
        self.check_button.setEnabled(ready and not running)
        if running:
            return
        self.status.setText(status_text(self.dicom_root, self.runs, self.checks))

    def start(self) -> None:
        if self.bids_root is None or self.dicom_root is None or self.worker is not None:
            return
        self.worker = CheckThread(self.bids_root, self.dicom_root, self.runs)
        self.worker.progress.connect(
            lambda done, total: self.status.setText(f"Reading DICOMs: run {done} of {total}...")
        )
        self.worker.done.connect(self.on_done)
        self.status.setText("Reading DICOMs...")
        self.worker.start()
        self.update_controls()

    def on_done(self, checks: list[dv.VolumeCheck], error: str) -> None:
        if self.worker is not None:
            self.worker.wait()
        self.worker = None
        self.checks = checks
        self.fill_table()
        self.update_controls()
        if error:
            self.status.setText(f"DICOMs not read: {error}")

    def fill_table(self) -> None:
        shown = [
            c for c in self.checks if not self.problems_only.isChecked() or c.result in PROBLEMS
        ]
        self.table.setRowCount(len(shown))
        for row, check in enumerate(shown):
            counts = check.counts
            cells = check_cells(check)
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(text))
            result = self.table.item(row, len(cells) - 1)
            if result is not None:
                result.setForeground(QColor(RESULT_COLOR[check.result]))
                result.setToolTip(wrap_tooltip(check.explanation))
            missing = self.table.item(row, 4)
            if missing is not None and counts is not None and counts.missing_images:
                missing.setForeground(QColor(ERROR_COLOR))
        self.table.resizeColumnsToContents()

    def set_dicom_root(self, dicom_root: Path | None) -> None:
        if dicom_root != self.dicom_root:
            self.set_folders(self.bids_root, dicom_root, self.runs)

    def stop(self) -> None:
        """Closing: finish the run being read, then stop."""
        if self.worker is not None:
            self.worker.job.stop()
            self.worker.wait()
