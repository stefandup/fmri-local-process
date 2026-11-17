"""The Sequence overview tab: a subject's runs of one scan (e.g. ses-1 and ses-2 of the
checkerboard) with their scan parameters side by side, as a terminal-style log. Rows where the
runs differ are marked '≠'. The values come from the NIfTI header and the BIDS sidecar; Read
DICOM headers fills in what those lack from the raw DICOM series (see sequence_params)."""

from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.gui.qt_common import (
    NoticeBanner,
    bids_notices,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.gui.toolbox_state import ToolboxState
from fmri_local_process.processing import sequence_params as sp


class ReadThread(QThread):
    """Headers and sidecars of a subject's runs; with a DICOM folder, their DICOMs too."""

    done = Signal(list, str)  # runs, error

    def __init__(self, runs: list[sp.ScanRun], bids_root: Path, dicom_root: Path | None) -> None:
        super().__init__()
        self.runs, self.bids_root, self.dicom_root = runs, bids_root, dicom_root

    def run(self) -> None:
        try:
            sp.read_values(self.runs)
            if self.dicom_root is not None:
                sp.add_dicom_values(self.runs, self.dicom_root, self.bids_root)
        except (OSError, ImportError) as e:
            self.done.emit(self.runs, str(e))
            return
        self.done.emit(self.runs, "")


class SequencePage(QWidget):
    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        self.seen_generation = -1
        self.seen_bids: Path | None = None
        self.runs: list[sp.ScanRun] = []
        self.dicom_read = False
        self.worker: ReadThread | None = None
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("Subject"))
        self.subject_combo = QComboBox()
        self.subject_combo.setMinimumContentsLength(14)
        self.subject_combo.currentTextChanged.connect(self.load_subject)
        row.addWidget(self.subject_combo)
        row.addWidget(QLabel("Scan"))
        self.scan_combo = QComboBox()
        self.scan_combo.setMinimumContentsLength(22)
        self.scan_combo.currentTextChanged.connect(self.show_table)
        row.addWidget(self.scan_combo)
        self.only_differences = QCheckBox("Only rows that differ")
        self.only_differences.toggled.connect(self.show_table)
        row.addWidget(self.only_differences)
        row.addStretch(1)
        self.dicom_button = QPushButton("Read DICOM headers")
        self.dicom_button.setToolTip(
            wrap_tooltip(
                "Find each run's raw DICOM series (in the DICOM folder chosen on the first tab, "
                "as the converter matched them) and read one file's header: fills in what the "
                "NIfTI header and sidecar lack, and adds DICOM-only fields (acquisition matrix, "
                "phase FOV, averages, echo train length). Patient fields are never read."
            )
        )
        self.dicom_button.clicked.connect(self.on_read_dicom)
        row.addWidget(self.dicom_button)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        self.save_button = QPushButton("Save...")
        self.save_button.setToolTip("Save the table as text (as shown) or CSV.")
        self.save_button.clicked.connect(self.on_save)
        row.addWidget(self.save_button)
        layout.addLayout(row)

        self.banner = NoticeBanner()
        layout.addWidget(self.banner)
        self.status = QLabel()
        style_secondary_label(self.status)
        layout.addWidget(self.status)
        self.log = QPlainTextEdit()  # a log, in a typewriter font
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Courier New"))
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log, 1)
        self.refresh()

    # ---- loading ----------------------------------------------------------------------------

    def show_page(self) -> None:
        if self.seen_generation != self.state.generation or self.seen_bids != self.state.folder(
            "bids"
        ):
            self.refresh()

    def refresh(self) -> None:
        self.seen_generation = self.state.generation
        bids_root = self.seen_bids = self.state.folder("bids")
        current = self.subject_combo.currentText()
        names = sp.subjects(bids_root) if bids_root is not None and bids_root.is_dir() else []
        self.subject_combo.blockSignals(True)
        self.subject_combo.clear()
        self.subject_combo.addItems(names)
        if current in names:
            self.subject_combo.setCurrentText(current)
        self.subject_combo.blockSignals(False)
        self.banner.set_messages(bids_notices(bids_root, bool(names)))
        if bids_root is None:
            self.runs = []
            self.log.setPlainText("Choose the BIDS folder on the Setup & Convert tab.")
            self.update_buttons()
            return
        self.load_subject()

    def load_subject(self) -> None:
        bids_root = self.state.folder("bids")
        subject = self.subject_combo.currentText()
        if bids_root is None or not subject or self.worker is not None:
            self.runs = []
            self.show_table()
            return
        self.start(sp.find_runs(bids_root, subject), dicom=False)

    def on_read_dicom(self) -> None:
        if self.state.folder("dicom") is None:
            QMessageBox.information(
                self, "No DICOM folder", "Choose the DICOM folder on the Setup & Convert tab."
            )
            return
        self.start(self.runs, dicom=True)

    def start(self, runs: list[sp.ScanRun], dicom: bool) -> None:
        bids_root = self.state.folder("bids")
        if bids_root is None or self.worker is not None:
            return
        self.dicom_read = dicom
        self.status.setText("Reading DICOM headers..." if dicom else "Reading headers...")
        self.worker = ReadThread(runs, bids_root, self.state.folder("dicom") if dicom else None)
        self.worker.done.connect(self.on_read)
        self.update_buttons()
        self.worker.start()

    def on_read(self, runs: list[sp.ScanRun], error: str) -> None:
        if self.worker is not None:
            self.worker.wait()
        self.worker = None
        self.runs = runs
        if runs and runs[0].subject != self.subject_combo.currentText():
            self.load_subject()  # the subject was changed while reading
            return
        if error:
            QMessageBox.warning(self, "Not all headers read", error)
        current = self.scan_combo.currentText()
        kinds = sp.scans(runs)
        self.scan_combo.blockSignals(True)
        self.scan_combo.clear()
        self.scan_combo.addItems(kinds)
        if current in kinds:
            self.scan_combo.setCurrentText(current)
        self.scan_combo.blockSignals(False)
        self.show_table()

    # ---- showing ----------------------------------------------------------------------------

    def shown_runs(self) -> list[sp.ScanRun]:
        scan = self.scan_combo.currentText()
        return [r for r in self.runs if r.scan == scan]

    def show_table(self) -> None:
        runs = self.shown_runs()
        subject = self.subject_combo.currentText()
        if not runs:
            self.log.setPlainText("No runs found for this subject." if subject else "")
        else:
            rows = sp.table_rows(runs)
            title = f"Sequence overview: sub-{subject}, {self.scan_combo.currentText()}"
            lines = [
                title,
                "=" * len(title),
                *sp.summary_lines(runs, rows),
                "",
                *sp.render_table(runs, rows, self.only_differences.isChecked()),
            ]
            self.log.setPlainText("\n".join(lines))
        self.status.setText(self.status_text())
        self.update_buttons()

    def status_text(self) -> str:
        if self.dicom_read:
            return "DICOM headers read for these runs."
        if self.state.folder("dicom") is None:
            return "From the NIfTI headers and BIDS sidecars (no DICOM folder chosen)."
        return "From the NIfTI headers and BIDS sidecars. Read DICOM headers to fill gaps."

    def update_buttons(self) -> None:
        idle = self.worker is None
        self.dicom_button.setEnabled(idle and bool(self.runs))
        self.save_button.setEnabled(idle and bool(self.shown_runs()))

    def on_save(self) -> None:
        runs = self.shown_runs()
        bids_root = self.state.folder("bids")
        if not runs or bids_root is None:
            return
        name = f"sub-{runs[0].subject}_{self.scan_combo.currentText()}_sequence"
        start = str(bids_root.parent / f"{name}.txt")
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Save sequence overview", start, "Text (*.txt);;CSV files (*.csv)"
        )
        if not chosen:
            return
        path = Path(chosen)
        try:
            if path.suffix.lower() == ".csv":
                sp.write_csv(runs, sp.table_rows(runs), path)
            else:
                path.write_text(self.log.toPlainText() + "\n", encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "Not saved", str(e))
