"""Scan lengths & trimming: how many volumes each BOLD scan should have, and trimming the
runs that are longer (scans stopped by hand after the task).

The expected length is by default the (upper) median of all a scan's runs (with a single
run, the study's usual length); the human can set another number, which is saved in the BIDS
folder (code/expected_volumes.json) so it travels with the data. The crosscheck flags runs
shorter than that as short. A "From logs" column suggests a length from the
events.tsv files (last event + a margin, settable per task), to type in if it fits.
"""

from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.app.scan_lengths import (
    COLUMNS,
    HELP_TEXT,
    LONG_COLOR,
    MAX_MARGIN_S,
    MAX_VOLUMES,
    TRIMMED_COLOR,
    ScanLengths,
    length_note,
    no_scans_notices,
    trimmed_details,
    trimmed_note,
)
from fmri_local_process.gui.dicom_volumes_box import DicomVolumesBox
from fmri_local_process.gui.image_view import HelpToggle
from fmri_local_process.gui.qt_common import (
    PROCESS_ACCENT_COLOR,
    PROCESS_ACCENT_HOVER_COLOR,
    NoticeBanner,
    primary_action_stylesheet,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.gui.toolbox_state import ToolboxState
from fmri_local_process.gui.trimming import Trimmer
from fmri_local_process.processing.behav_match import LogInfo
from fmri_local_process.processing.crosscheck import (
    DataFile,
    RunEnd,
    find_data_files,
    log_event_ends,
    long_runs,
    review_bids_folder,
)


def trimmed_item(runs: list[DataFile]) -> QTableWidgetItem:
    item = QTableWidgetItem(trimmed_note(runs))
    item.setToolTip(trimmed_details(runs))
    item.setForeground(QColor(TRIMMED_COLOR))
    return item


AUTOSAVE_DELAY_MS = 800  # save once typing pauses, not on every digit


class ScanLengthsPanel(QWidget):
    saved = Signal()
    trim_wanted = Signal()  # saved, and the long runs should now be trimmed

    EXPECTED, FROM_LOGS, MARGIN, MINUTES, TOO_LONG, TRIMMED = 3, 4, 5, 6, 7, 8

    def __init__(
        self,
        bids_root: Path,
        data_files: list[DataFile],
        parent: QWidget | None,
        can_trim: bool = True,
        log_ends: dict[str, list[RunEnd]] | None = None,
    ):
        super().__init__(parent)
        self.lengths = ScanLengths(bids_root, data_files, log_ends)
        self.bids_root, self.scans = bids_root, self.lengths.scans
        self.margins: dict[str, QDoubleSpinBox] = {}
        self.trim_requested = False
        self.can_trim = can_trim
        self.autosave = QTimer(self)
        self.autosave.setSingleShot(True)
        self.autosave.setInterval(AUTOSAVE_DELAY_MS)
        self.autosave.timeout.connect(self.save)

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.trim_button = QPushButton("Trim long runs...")
        self.trim_button.setToolTip(
            wrap_tooltip(
                "Cut every run that is longer than its expected length down to it, keeping "
                "the first volumes (you'll be asked to confirm)."
            )
        )
        self.trim_button.setStyleSheet(
            primary_action_stylesheet(PROCESS_ACCENT_COLOR, PROCESS_ACCENT_HOVER_COLOR)
        )
        self.trim_button.clicked.connect(self.trim)
        top.addWidget(self.trim_button)
        top.addStretch(1)
        self.saved_label = QLabel()
        style_secondary_label(self.saved_label)
        top.addWidget(self.saved_label)
        self.help = HelpToggle("Help ?", "What the expected lengths do, and trimming.")
        self.help.set_text(f"{HELP_TEXT}<br><br>{self.lengths.saved_text()}")
        top.addWidget(self.help.button)
        layout.addLayout(top)
        layout.addWidget(self.help.box)

        self.table = QTableWidget(len(self.scans), len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.spins: dict[str, QSpinBox] = {}
        for row, (label, runs) in enumerate(self.scans.items()):
            cells = (label, str(len(runs)), length_note(runs))
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(text))
            self.table.setItem(row, self.TRIMMED, trimmed_item(runs))
            spin = QSpinBox()
            spin.setRange(0, MAX_VOLUMES)
            spin.setSpecialValueText(self.lengths.automatic_text(label))
            spin.setValue(self.lengths.set[label])
            spin.valueChanged.connect(lambda _=0, r=row, lab=label: self.on_changed(r, lab))
            self.table.setCellWidget(row, self.EXPECTED, spin)
            self.spins[label] = spin
            if label in self.lengths.margins:
                margin = QDoubleSpinBox()
                margin.setRange(0, MAX_MARGIN_S)
                margin.setDecimals(1)
                margin.setSuffix(" s")
                margin.setValue(self.lengths.margins[label])
                margin.setToolTip(
                    "Time kept after the last event when suggesting a length from the logs."
                )
                margin.valueChanged.connect(
                    lambda _=0.0, r=row, lab=label: self.on_margin_changed(r, lab)
                )
                self.table.setCellWidget(row, self.MARGIN, margin)
                self.margins[label] = margin
            self.update_from_logs(row, label)
            self.update_row(row, label)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.update_trim_button()

    def too_long(self, label: str) -> list[DataFile]:
        return self.lengths.too_long(label)

    def update_from_logs(self, row: int, label: str) -> None:
        """The 'From logs' cell: the suggested volumes at the margin set for the scan."""
        text, why = self.lengths.from_logs(label)
        item = QTableWidgetItem(text)
        item.setToolTip(why)
        self.table.setItem(row, self.FROM_LOGS, item)

    def on_margin_changed(self, row: int, label: str) -> None:
        self.lengths.margins[label] = self.margins[label].value()
        self.update_from_logs(row, label)
        self.saved_label.setText("Saving...")
        self.autosave.start()

    def on_changed(self, row: int, label: str) -> None:
        self.lengths.set[label] = self.spins[label].value()
        self.update_row(row, label)
        self.saved_label.setText("Saving...")
        self.autosave.start()  # restarts while typing: saves once typing pauses

    def update_row(self, row: int, label: str) -> None:
        self.table.setItem(row, self.MINUTES, QTableWidgetItem(self.lengths.minutes(label)))
        long_runs = self.too_long(label)
        item = QTableWidgetItem(str(len(long_runs)) if long_runs else "")
        item.setToolTip(self.lengths.too_long_details(label))
        item.setForeground(QColor(LONG_COLOR))
        self.table.setItem(row, self.TOO_LONG, item)
        if len(self.spins) == len(self.scans):  # not while the rows are still being built
            self.update_trim_button()

    def update_trim_button(self) -> None:
        count = self.lengths.long_count()
        self.trim_button.setText(f"Trim {count} long run(s)...")
        self.trim_button.setEnabled(self.can_trim and count > 0)
        if not self.can_trim:
            self.trim_button.setToolTip("Can't trim now: another job is changing the BIDS folder.")

    def save(self) -> None:
        self.autosave.stop()
        self.lengths.save()
        self.saved_label.setText("Lengths saved")
        self.saved.emit()

    def trim(self) -> None:
        """Save what was typed, then the page trims (after confirming) to those lengths."""
        self.trim_requested = True
        self.save()
        self.trim_wanted.emit()


class ScanLengthsPage(QWidget):
    """The Scan lengths & trimming tab: the panel above for the current BIDS folder,
    rebuilt when the folder changes; trimming goes through the shared Trimmer."""

    def __init__(self, state: ToolboxState, trimmer: Trimmer) -> None:
        super().__init__()
        self.state, self.trimmer = state, trimmer
        self.seen_generation = -1
        self.panel: ScanLengthsPanel | None = None
        self.log_infos: list[LogInfo] = []  # read by the Behaviour logs tab, for "From logs"
        self.layout_ = QVBoxLayout(self)
        self.banner = NoticeBanner()
        self.layout_.addWidget(self.banner)
        self.filler = QWidget()  # keeps the banner at the top while there is no panel
        self.layout_.addWidget(self.filler, 1)
        self.dicom_box = DicomVolumesBox()  # stays; the panel goes in above it
        self.layout_.addWidget(self.dicom_box, 1)
        state.busy_changed.connect(self.update_lock)
        state.folder_changed.connect(
            lambda key: (
                self.dicom_box.set_dicom_root(state.folder("dicom")) if key == "dicom" else None
            )
        )

    def show_page(self) -> None:
        """Called when the tab is shown: rebuild if the BIDS folder changed since."""
        if self.seen_generation != self.state.generation:
            self.refresh()

    def set_log_infos(self, infos: list[LogInfo]) -> None:
        """The Behaviour logs tab (re)read the logs: suggest lengths from them."""
        self.log_infos = infos
        if self.panel is not None:
            self.refresh()

    def refresh(self) -> None:
        self.seen_generation = self.state.generation
        if self.panel is not None:
            if self.panel.autosave.isActive():
                self.panel.save()  # don't lose what was just typed
            self.layout_.removeWidget(self.panel)
            self.panel.deleteLater()
            self.panel = None
        bids_root = self.state.folder("bids")
        self.filler.setVisible(bids_root is None)
        data_files = find_data_files(bids_root) if bids_root is not None else []
        self.dicom_box.set_folders(bids_root, self.state.folder("dicom"), data_files)
        if bids_root is None:
            self.banner.set_messages(no_scans_notices(None, False))
            return
        self.panel = ScanLengthsPanel(
            bids_root, data_files, self, log_ends=log_event_ends(self.log_infos, data_files)
        )
        self.banner.set_messages(no_scans_notices(bids_root, bool(self.panel.scans)))
        self.panel.saved.connect(self.on_saved)
        self.panel.trim_wanted.connect(self.on_trim_wanted)
        self.layout_.insertWidget(self.layout_.indexOf(self.dicom_box), self.panel, 1)
        self.update_lock()

    def update_lock(self) -> None:
        if self.panel is not None:
            self.panel.can_trim = not self.state.lock_reason()
            self.panel.update_trim_button()

    def on_saved(self) -> None:
        # The crosscheck's short/long flags follow the saved lengths. This panel already
        # shows them: mark it current first, so the bump doesn't rebuild it under your typing.
        self.seen_generation = self.state.generation + 1
        self.state.bump()

    def on_trim_wanted(self) -> None:
        bids_root = self.state.folder("bids")
        if bids_root is not None:
            self.trimmer.start(self, long_runs(review_bids_folder(bids_root)))
