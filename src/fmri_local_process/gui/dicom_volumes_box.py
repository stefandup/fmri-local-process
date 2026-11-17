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

from fmri_local_process.gui.image_view import HelpToggle
from fmri_local_process.gui.qt_common import (
    ERROR_COLOR,
    OK_COLOR,
    UNKNOWN_COLOR,
    WARNING_COLOR,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.processing import dicom_volumes as dv
from fmri_local_process.processing.crosscheck import DataFile

RESULT_COLOR = {
    dv.MISSING: ERROR_COLOR,
    dv.DIFFERS: ERROR_COLOR,
    dv.INCOMPLETE: WARNING_COLOR,
    dv.STOPPED: WARNING_COLOR,
    dv.OK: OK_COLOR,
    dv.NOT_CHECKED: UNKNOWN_COLOR,
}
PROBLEMS = (dv.MISSING, dv.DIFFERS, dv.INCOMPLETE)
# (header, tooltip)
COLUMNS = (
    ("Run", "The BOLD run in the BIDS folder."),
    ("DICOM series", "The DICOM series folder it was converted from."),
    (
        "DICOM images",
        "How many DICOM image files the series folder holds. A Siemens mosaic file holds a "
        "whole volume; otherwise each file is one slice.",
    ),
    (
        "Instance numbers",
        "The range of the images' instance numbers (DICOM tag 0020,0013). The scanner numbers "
        "a series' images 1, 2, 3, ... with no gaps, so the range says how many images there "
        "should be. This is how dcm2niix spots missing images.",
    ),
    (
        "Missing images",
        "Images the instance numbers say should be there, but aren't: (last - first + 1) - "
        "images found. Not 0 means files were lost in the export or the copy: dcm2niix's "
        "'Missing images? Expected N images, but instance number ranges from a to b'.",
    ),
    (
        "Volumes (DICOM)",
        "Complete volumes in the DICOM folder: images / slice positions per volume (dcm2niix "
        "counts how often the first slice position repeats).",
    ),
    (
        "Planned (protocol)",
        "The volumes the scan protocol asked for (NumberOfTemporalPositions, or the Siemens "
        "protocol's repetitions + 1). Fewer acquired means the scan was stopped early, which "
        "isn't missing data. Blank when the scanner doesn't record it.",
    ),
    (
        "Volumes (NIfTI)",
        "The volumes dcm2niix wrote into the BIDS NIfTI (before any trim on this tab: "
        "trimming cuts volumes on purpose).",
    ),
    ("Result", "What the numbers mean: hover a run's result for the full sentence."),
)
HELP_TEXT = (
    "dcm2niix warns while converting when DICOM images seem to be missing. This redoes its "
    "checks per BOLD run, so they can be looked at afterwards:<br><br>"
    "<b>Missing images</b> (red): the scanner numbers a series' images without gaps "
    "(instance numbers 1, 2, 3, ...). A gap means files were lost between the scanner and "
    "this folder, so volumes (or slices of them) are missing. Export the series again.<br>"
    "<b>NIfTI differs</b> (red): the NIfTI doesn't have the volumes the DICOM folder holds: "
    "convert the run again.<br>"
    "<b>Incomplete volume</b> (orange): the last volume has only some of its slices: the scan "
    "was stopped mid-volume; dcm2niix leaves it out.<br>"
    "<b>Stopped early</b> (orange): every image is there, but fewer volumes than the "
    "protocol planned: the scan was stopped before its end. Nothing is lost; check the task "
    "still fits (Expected volumes above).<br><br>"
    "Reads a few header tags of every DICOM file (never the patient fields): a minute or so "
    "per few hundred runs."
)


class CheckThread(QThread):
    progress = Signal(int, int)
    done = Signal(list, str)  # checks, error

    def __init__(self, bids_root: Path, dicom_root: Path, runs: list[DataFile]) -> None:
        super().__init__()
        self.bids_root, self.dicom_root, self.runs = bids_root, dicom_root, runs
        self.stop_requested = False

    def run(self) -> None:
        try:
            checks = dv.check_runs(
                self.bids_root,
                self.dicom_root,
                self.runs,
                self.progress.emit,
                lambda: self.stop_requested,
            )
        except (OSError, ImportError) as e:
            self.done.emit([], str(e))
            return
        self.done.emit(checks, "")


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
        if self.dicom_root is None:
            self.status.setText("Choose the DICOM sessions folder (Setup tab) to check.")
        elif not self.runs:
            self.status.setText("No BOLD runs to check.")
        elif not self.checks:
            self.status.setText(f"{len(self.runs)} BOLD run(s) not checked yet.")
        else:
            problems = sum(1 for c in self.checks if c.result in PROBLEMS)
            stopped = sum(1 for c in self.checks if c.result == dv.STOPPED)
            self.status.setText(
                f"{len(self.checks)} run(s) checked: {problems} with missing or unconverted "
                f"volumes, {stopped} stopped early."
            )

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
            cells = [check.name, check.series or "-"]
            if counts is None:
                cells += ["", "", "", "", ""]
            else:
                instances = (
                    f"{counts.first_instance}-{counts.last_instance}"
                    if counts.first_instance is not None
                    else "-"
                )
                cells += [
                    str(counts.images),
                    instances,
                    str(counts.missing_images),
                    str(counts.volumes),
                    "" if counts.planned is None else str(counts.planned),
                ]
            cells += [str(check.nifti_volumes or ""), check.result]
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
            self.worker.stop_requested = True
            self.worker.wait()
