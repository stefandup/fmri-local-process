"""fmri_crosscheck: review a converted Philani BIDS folder (PySide6).

Modelled on vr_lab_toolbox's BIDS crosscheck tools: sessions on the left, the selected
session's files and problems on the right, and the converter one click away. All checks
read the BIDS folder; nothing but the human's own moves/deletes is written to it.
"""

import logging
import sys
from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QStandardPaths, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
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

from fmri_local_process.gui.behav_logs_dialog import BehavLogsDialog
from fmri_local_process.gui.qt_common import (
    PROCESS_ACCENT_COLOR,
    accent_group_box_stylesheet,
    style_secondary_label,
)
from fmri_local_process.gui.scan_lengths import ScanLengthsDialog
from fmri_local_process.processing.crosscheck import (
    CrosscheckError,
    DataFile,
    SessionReview,
    delete_data_file,
    find_data_files,
    keep_run,
    review_bids_folder,
    sidecar_note,
    trim_run,
)
from fmri_local_process.processing.dcm2niix import (
    Dcm2niixNotFoundError,
    find_dcm2niix,
    run_dcm2niix,
)
from fmri_local_process.processing.deface import DefaceUnavailableError, make_deface
from fmri_local_process.processing.philani_bids import ConversionReport, convert_dicom_root

log = logging.getLogger(__name__)

SETTINGS_ORGANIZATION = "MooiToolbox"  # same as vr_lab_toolbox
SETTINGS_APP = "FmriCrosscheck"
ICONS = {"pending": "⚠", "short": "❗", "long": "✂", "missing": "○", "check": "⚑"}
COLORS = {
    "pending": "#f39c12",
    "short": "#e74c3c",
    "long": "#3498db",
    "missing": "#9b59b6",
    "check": "#e67e22",
    "ok": "#2ecc71",
}
TABLE_COLUMNS = ("ID", "Date", "Session", "Files", "Problems")
PROBLEMS_COLUMN = 4
OK_ICON = "●"
ACCENT_COLOR = "#5b9bd5"
LEGEND = (
    f"{OK_ICON} nothing to resolve\n"
    f"{ICONS['pending']} a scan converted as several runs: keep one\n"
    f"{ICONS['short']} shorter than expected (or than the same scan elsewhere), or no events\n"
    f"{ICONS['long']} longer than expected: trim it\n"
    f"{ICONS['missing']} expected file missing\n"
    f"{ICONS['check']} events from a log dated other than the scan: check it's the right log"
)


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


class TrimThread(QThread):
    """Trims runs one by one (each is a few seconds of gzip); reports progress as log lines."""

    line = Signal(str)

    def __init__(self, jobs: list[tuple[DataFile, int]]):
        super().__init__()
        self.jobs = jobs

    def run(self) -> None:
        for number, (data, volumes) in enumerate(self.jobs, start=1):
            try:
                trim_run(data, volumes)
            except (CrosscheckError, OSError, ValueError) as e:
                self.line.emit(f"NOT TRIMMED  {e}")
                continue
            self.line.emit(
                f"Trimmed {data.path.name} to {volumes} volumes ({number}/{len(self.jobs)})"
            )


def reveal(folder: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def problem_text(review: SessionReview) -> str:
    counts = {kind: sum(i.kind == kind for i in review.issues) for kind in ICONS}
    parts = [f"{ICONS[kind]} {n}" for kind, n in counts.items() if n]
    return "   ".join(parts) or f"{OK_ICON} ok"


def problem_color(review: SessionReview) -> str:
    kinds = {i.kind for i in review.issues}
    order = ("short", "pending", "check", "long", "missing")
    return next((COLORS[k] for k in order if k in kinds), COLORS["ok"])


class CrosscheckWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("fMRI BIDS Crosscheck")
        self.resize(1300, 850)
        self.settings = QSettings(SETTINGS_ORGANIZATION, SETTINGS_APP)
        self.folders: dict[str, Path | None] = {}
        self.reviews: list[SessionReview] = []
        self.convert_thread: ConvertThread | None = None
        self.trim_thread: TrimThread | None = None
        self.emitter = LogEmitter()
        self.emitter.line.connect(self.log_line)
        self.log_handler = QtLogHandler(self.emitter)
        self._build_ui()
        for key in ("bids", "dicom", "behav"):
            saved = str(self.settings.value(key, "") or "")
            self.set_folder(key, Path(saved) if saved and Path(saved).is_dir() else None)
        self.rescan()

    # ---- layout -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)

        folders = QGroupBox("Folders")
        folders.setStyleSheet(
            f"QGroupBox {{ border: 2px solid {ACCENT_COLOR}; border-radius: 4px; "
            "margin-top: 8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
        )
        grid = QGridLayout(folders)
        self.folder_labels: dict[str, QLabel] = {}
        rows = (
            ("bids", "BIDS folder (checked)", "The converted BIDS dataset to crosscheck."),
            ("dicom", "DICOM sessions (raw)", "Only needed to convert; never modified."),
            (
                "behav",
                "Behavioural logs (raw)",
                "Optional: the top folder of the behavioural logs. Every .log below it, in any "
                "subfolder, is found; the task comes from 'cogreappraisal'/'emolabeling' in its "
                "path. Never modified.",
            ),
        )
        for row, (key, title, tip) in enumerate(rows):
            caption = QLabel(f"<b>{title}</b>")
            caption.setToolTip(tip)
            label = QLabel()
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.folder_labels[key] = label
            browse = QPushButton("Browse...")
            browse.clicked.connect(partial(self.on_browse, key))
            reveal_button = QPushButton("Reveal")
            reveal_button.clicked.connect(partial(self.on_reveal_folder, key))
            grid.addWidget(caption, row, 0)
            grid.addWidget(label, row, 1)
            grid.addWidget(browse, row, 2)
            grid.addWidget(reveal_button, row, 3)
        grid.setColumnStretch(1, 1)

        actions = QHBoxLayout()
        rescan = QPushButton("Rescan BIDS folder")
        rescan.clicked.connect(self.rescan)
        actions.addWidget(rescan)
        lengths = QPushButton("Scan lengths && trimming...")
        lengths.setToolTip(
            "How many volumes each BOLD scan should have (median of its runs, or your own "
            "number), and trimming the runs that are longer."
        )
        lengths.clicked.connect(self.on_scan_lengths)
        actions.addWidget(lengths)
        behav = QPushButton("IDs && behaviour logs...")
        behav.setToolTip(
            "Match each behavioural log to a scan session by ID and date (scan dates and times "
            "shown, e.g. for twins); correct wrong IDs in log or DICOM folder names, or wrong "
            "dates; see incomplete logs and what the logs contain."
        )
        behav.clicked.connect(self.on_behav_logs)
        actions.addWidget(behav)
        actions.addStretch(1)
        self.deface_box = QCheckBox("Deface T1w")
        self.deface_box.setToolTip(
            "Uses mri_deface from $FREESURFER_HOME, else the defacer the installer set up in "
            "Docker (FreeSurfer mri_deface or pydeface)."
        )
        actions.addWidget(self.deface_box)
        self.convert_button = QPushButton("Convert / update BIDS")
        self.convert_button.setToolTip(
            "Run the DICOM -> BIDS converter. Existing files are kept; deleted ones are "
            "recreated from the raw data."
        )
        self.convert_button.setStyleSheet(
            f"QPushButton {{ font-weight: bold; padding: 6px 20px; background-color: "
            f"{ACCENT_COLOR}; color: white; border-radius: 4px; }}"
            "QPushButton:disabled { background-color: #555555; color: #999999; }"
        )
        self.convert_button.clicked.connect(self.on_convert)
        actions.addWidget(self.convert_button)
        grid.addLayout(actions, len(rows), 0, 1, 4)
        layout.addWidget(folders)
        layout.addWidget(self._build_progress_group())

        splitter = QSplitter(Qt.Orientation.Horizontal)
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
        splitter.addWidget(self.table)

        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        splitter.addWidget(self.detail_scroll)
        splitter.setSizes([380, 920])
        layout.addWidget(splitter, 1)

        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.activity = QTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumHeight(160)
        layout.addWidget(self.activity)
        self.setCentralWidget(central)

    def _build_progress_group(self) -> QGroupBox:
        """Large progress panel (as in the preprocess GUI), shown only while converting."""
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

    def on_convert_progress(self, done: int, total: int, what: str) -> None:
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)
        self.progress_bar.setFormat("%v / %m steps (%p%)")
        self.progress_label.setText(f"Now: {what}" if done < total else "Finishing up...")

    # ---- folders --------------------------------------------------------------------------

    def set_folder(self, key: str, folder: Path | None) -> None:
        self.folders[key] = folder
        self.folder_labels[key].setText(str(folder) if folder else "<i>not set</i>")
        self.settings.setValue(key, str(folder) if folder else "")
        self.convert_button.setEnabled(
            bool(self.folders.get("bids") and self.folders.get("dicom"))
            and self.convert_thread is None
        )

    def on_browse(self, key: str) -> None:
        current = self.folders.get(key)
        start = (
            str(current)
            if current
            else QStandardPaths.writableLocation(QStandardPaths.StandardLocation.HomeLocation)
        )
        chosen = QFileDialog.getExistingDirectory(self, "Choose folder", start)
        if not chosen:
            return
        self.set_folder(key, Path(chosen))
        if key == "bids":
            self.rescan()

    def on_reveal_folder(self, key: str) -> None:
        folder = self.folders.get(key)
        if folder:
            reveal(folder)

    # ---- scanning ---------------------------------------------------------------------------

    def rescan(self) -> None:
        bids_root = self.folders.get("bids")
        selected = self.selected_review()
        self.reviews = review_bids_folder(bids_root) if bids_root else []
        self.table.setRowCount(len(self.reviews))
        for row, review in enumerate(self.reviews):
            cells = (
                review.subject,
                review.date or "?",
                f"ses-{review.session}" if review.session else "-",
                str(len(review.files) + len(review.pending)),
                problem_text(review),
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 1 and review.source_folder:
                    item.setToolTip(f"Converted from {review.source_folder}")
                if column == PROBLEMS_COLUMN:
                    item.setForeground(QColor(problem_color(review)))
                    item.setToolTip(LEGEND)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        with_issues = sum(bool(r.issues) for r in self.reviews)
        self.summary.setText(
            f"{len(self.reviews)} session(s), {with_issues} with something to check"
            if bids_root
            else "Choose a BIDS folder."
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

    # ---- detail pane -------------------------------------------------------------------------

    def render_detail(self) -> None:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        review = self.selected_review()
        bids_root = self.folders.get("bids")
        if review is None or bids_root is None:
            layout.addWidget(QLabel("Select a session."))
        else:
            scanned = f"scanned {review.date}" if review.date else "scan date unknown"
            source = f" from {review.source_folder}" if review.source_folder else ""
            title = QLabel(f"<h2>{review.name}</h2>{scanned}{source}")
            layout.addWidget(title)
            if review.issues:
                layout.addWidget(self.issues_group(review))
            for label in sorted({d.label for d in review.pending}):
                candidates = [d for d in review.pending if d.label == label]
                layout.addWidget(self.pick_run_group(label, candidates, bids_root))
            if review.files:
                layout.addWidget(self.files_group(review, bids_root))
        layout.addStretch(1)
        self.detail_scroll.setWidget(widget)

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
                trim.setEnabled(self.trim_thread is None)
                trim.clicked.connect(partial(self.on_trim, [(data, issue.trim_to)]))
                grid.addWidget(trim, row, 5)
            reveal_button = QPushButton("Reveal")
            reveal_button.clicked.connect(partial(reveal, data.path.parent))
            grid.addWidget(reveal_button, row, 3)
            delete = QPushButton("Delete...")
            delete.setToolTip(
                "Delete this file and its sidecar. 'Convert / update BIDS' recreates it from "
                "the raw data, e.g. once an incomplete copy has been redone."
            )
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

    def on_scan_lengths(self) -> None:
        bids_root = self.folders.get("bids")
        if bids_root is None:
            QMessageBox.information(self, "No BIDS folder", "Choose the BIDS folder first.")
            return
        dialog = ScanLengthsDialog(
            bids_root, find_data_files(bids_root), self, can_trim=self.trim_thread is None
        )
        if not dialog.exec():
            return
        self.log_line("Saved expected scan lengths")
        self.rescan()  # the long-run issues now follow the saved lengths
        if dialog.trim_requested:
            self.on_trim(self.long_runs())

    def on_behav_logs(self) -> None:
        roots = [self.folders.get(key) for key in ("bids", "behav", "dicom")]
        bids_root, behav_root, dicom_root = roots
        if bids_root is None or behav_root is None or dicom_root is None:
            QMessageBox.information(
                self,
                "Folders needed",
                "Choose the BIDS folder, the DICOM sessions and the behavioural logs first.",
            )
            return
        try:
            dialog = BehavLogsDialog(bids_root, behav_root, dicom_root, self)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Can't read the logs", str(e))
            return
        if dialog.exec():
            self.log_line("Saved ID / log corrections: Convert / update BIDS to apply them")

    def long_runs(self) -> list[tuple[DataFile, int]]:
        files = {d.path: d for r in self.reviews for d in r.files}
        return [
            (files[i.path], i.trim_to)
            for r in self.reviews
            for i in r.issues
            if i.kind == "long" and i.trim_to and i.path in files
        ]

    def on_trim(self, jobs: list[tuple[DataFile, int]]) -> None:
        if not jobs or self.trim_thread is not None:
            return
        names = "\n".join(f"  {d.path.name}: {d.volumes} -> {n}" for d, n in jobs[:15])
        more = f"\n  ... and {len(jobs) - 15} more" if len(jobs) > 15 else ""
        if not self.confirm(
            f"Trim {len(jobs)} run(s), keeping the first volumes?\n{names}{more}\n\n"
            "The cut volumes are deleted from the BIDS folder (the raw DICOMs keep them: "
            "delete the file and convert again to restore it). Don't trim while C-PAC is "
            "preprocessing these subjects."
        ):
            return
        self.trim_thread = TrimThread(jobs)
        self.trim_thread.line.connect(self.log_line)
        self.trim_thread.finished.connect(self.on_trim_done)
        self.trim_thread.start()

    def on_trim_done(self) -> None:
        if self.trim_thread is not None:
            self.trim_thread.wait()
        self.trim_thread = None
        self.rescan()

    def on_convert(self) -> None:
        bids_root, dicom_root = self.folders.get("bids"), self.folders.get("dicom")
        if bids_root is None or dicom_root is None:
            return
        self.convert_button.setEnabled(False)
        self.log_line(f"Converting {dicom_root} -> {bids_root} ...")
        logging.getLogger("fmri_local_process").addHandler(self.log_handler)
        self.convert_thread = ConvertThread(
            dicom_root, bids_root, self.folders.get("behav"), self.deface_box.isChecked()
        )
        self.convert_thread.done.connect(self.on_convert_done)
        self.convert_thread.finished.connect(self.on_convert_finished)
        self.convert_thread.progress.connect(self.on_convert_progress)
        self.progress_bar.setRange(0, 0)  # busy until the first step: sessions are being found
        self.progress_label.setText("Finding sessions and behavioural logs...")
        self.progress_group.setVisible(True)
        self.convert_thread.start()

    def on_convert_finished(self) -> None:
        """The thread ended without reporting (an unexpected error): don't leave the window
        stuck with Convert disabled. `done` always arrives first when the run completes."""
        if self.convert_thread is not None:
            self.on_convert_done(
                "The converter stopped unexpectedly. The error is in the console window; "
                "please pass it on."
            )

    def on_convert_done(self, result: ConversionReport | str) -> None:
        logging.getLogger("fmri_local_process").removeHandler(self.log_handler)
        if self.convert_thread is not None:
            self.convert_thread.wait()
        self.convert_thread = None
        self.progress_group.setVisible(False)
        self.set_folder("dicom", self.folders.get("dicom"))  # re-enables the button
        if isinstance(result, str):
            QMessageBox.warning(self, "Conversion failed", result)
            self.log_line(f"ERROR  {result}")
        else:
            self.log_line(
                f"Done: {len(result.converted)} converted, {len(result.to_pick)} runs to pick, "
                f"{len(result.unplaced)} log(s) not placed, {len(result.flagged)} incomplete, "
                f"{len(result.failures)} failed."
            )
            for change in result.renumbered:
                self.log_line(f"RENUMBERED  {change} (an earlier session was added)")
            for log_key, why in result.unplaced:
                self.log_line(f"NOT PLACED  {log_key}: {why} -- see IDs & behaviour logs")
            for what, why in result.failures:
                self.log_line(f"FAILED  {what}: {why}")
        self.rescan()

    def log_line(self, text: str) -> None:
        self.activity.append(text)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = QApplication(sys.argv)
    window = CrosscheckWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
