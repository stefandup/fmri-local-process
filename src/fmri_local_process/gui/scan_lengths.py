"""Scan lengths & trimming: how many volumes each BOLD scan should have, and trimming the
runs that are longer (scans stopped by hand after the task).

The expected length is by default the (upper) median of all a scan's runs (with a single
run, the study's usual length); the human can set another number, which is saved in the BIDS
folder (code/expected_volumes.json) so it travels with the data. The crosscheck flags runs
shorter than that as short. A "From logs" column suggests a length from the
events.tsv files (last event + a margin, settable per task), to type in if it fits.
"""

from collections import Counter
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
    DEFAULT_EVENTS_MARGIN_S,
    EXPECTED_FILE,
    SETTINGS_FILE,
    DataFile,
    RunEnd,
    automatic_lengths,
    find_data_files,
    log_event_ends,
    long_runs,
    read_events_margins,
    read_expected,
    review_bids_folder,
    run_event_ends,
    volumes_from_logs,
    write_events_margins,
    write_expected,
)

MAX_VOLUMES = 100_000
MAX_MARGIN_S = 120.0
LONG_COLOR = "#3498db"  # the crosscheck's colour for "long"
TRIMMED_COLOR = "#27ae60"  # done: green


def scan_lengths(data_files: list[DataFile]) -> dict[str, list[DataFile]]:
    """The 4D scans in the BIDS folder (not staged ones), by label."""
    scans: dict[str, list[DataFile]] = {}
    for data in data_files:
        if not data.pending and data.volumes is not None and data.volumes > 1:
            scans.setdefault(data.label, []).append(data)
    return dict(sorted(scans.items()))


def length_note(runs: list[DataFile]) -> str:
    """'441 (x3), 520, 33' -- the commonest lengths first."""
    counts = Counter(d.volumes for d in runs).most_common()
    return ", ".join(f"{n} (x{c})" if c > 1 else str(n) for n, c in counts)


def trimmed_note(runs: list[DataFile]) -> str:
    """'Trimmed 3 of 5' (all: 'Trimmed (all 5)'), or '' when none of the runs was trimmed."""
    trimmed = sum(1 for d in runs if d.trimmed_from)
    if not trimmed:
        return ""
    return (
        f"Trimmed (all {trimmed})" if trimmed == len(runs) else f"Trimmed {trimmed} of {len(runs)}"
    )


def trimmed_item(runs: list[DataFile]) -> QTableWidgetItem:
    item = QTableWidgetItem(trimmed_note(runs))
    item.setToolTip(
        "\n".join(
            f"{d.path.name}: {d.trimmed_from} -> {d.volumes} volumes"
            for d in runs
            if d.trimmed_from
        )
    )
    item.setForeground(QColor(TRIMMED_COLOR))
    return item


HELP_TEXT = (
    "How many volumes each BOLD scan should have. By default the median of its runs (with a "
    "single run, the study's usual length). The checkerboard instead uses its task design: "
    "200 s at the runs' TR, rounded up (task design). Type another number to set "
    "it, or step down to the bottom to go back to the automatic length; it is saved as you "
    "type.<br><br>"
    "Runs with fewer volumes are flagged as short at the BIDS Crosscheck. Runs with more can "
    "be trimmed to this length with <b>Trim</b>, keeping the first volumes: the cut volumes "
    "are deleted from the BIDS folder (the raw DICOMs keep them; delete the file and convert "
    "again to undo). Trim here, before the crosscheck, so BIDS is right before it is "
    "preprocessed.<br><br>"
    "<b>From logs</b> suggests a length from the complete behaviour logs (read on the "
    "Behaviour logs tab; logs marked error / not used left out; until they are read, the "
    "events.tsv files in BIDS): each log's last event end plus the <b>Margin</b> (default "
    f"{DEFAULT_EVENTS_MARGIN_S:g} s, so the BOLD response to the last trials is kept), in "
    "volumes at its TR; the median over runs, so one log that stopped early doesn't set it. "
    "It's only a suggestion: type it into Expected volumes if it fits. Scans without logs "
    "(e.g. rest) show none."
)
AUTOSAVE_DELAY_MS = 800  # save once typing pauses, not on every digit


class ScanLengthsPanel(QWidget):
    saved = Signal()
    trim_wanted = Signal()  # saved, and the long runs should now be trimmed

    COLUMNS = (
        "Scan",
        "Runs",
        "Volumes found",
        "Expected volumes",
        "From logs",
        "Margin (s)",
        "Minutes",
        "Too long",
        "Trimmed",
    )
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
        self.bids_root = bids_root
        self.scans = scan_lengths(data_files)
        self.automatic = automatic_lengths(data_files)  # median of runs, else study default
        # Read once (margins only rescale them): the complete raw logs the Behaviour logs tab
        # read, else the events.tsv files already in BIDS.
        self.log_ends = log_ends or {}
        self.event_ends = self.log_ends | {
            label: ends
            for label, ends in run_event_ends(data_files).items()
            if label not in self.log_ends
        }
        margins = read_events_margins(bids_root)
        self.margins: dict[str, QDoubleSpinBox] = {}
        self.trim_requested = False
        self.can_trim = can_trim
        expected = read_expected(bids_root)
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
        self.help.set_text(
            HELP_TEXT + f"<br><br>Saved in {bids_root / EXPECTED_FILE} (margins in "
            f"{bids_root / SETTINGS_FILE})."
        )
        top.addWidget(self.help.button)
        layout.addLayout(top)
        layout.addWidget(self.help.box)

        self.table = QTableWidget(len(self.scans), len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
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
            if label in self.automatic:
                volumes, source = self.automatic[label]
                kind = "median" if source == "median of runs" else source
                spin.setSpecialValueText(f"{kind} ({volumes})")
            else:
                spin.setSpecialValueText("not set")
            spin.setValue(expected.get(label, 0))
            spin.valueChanged.connect(lambda _=0, r=row, lab=label: self.on_changed(r, lab))
            self.table.setCellWidget(row, self.EXPECTED, spin)
            self.spins[label] = spin
            if label in self.event_ends:
                margin = QDoubleSpinBox()
                margin.setRange(0, MAX_MARGIN_S)
                margin.setDecimals(1)
                margin.setSuffix(" s")
                margin.setValue(margins.get(label, DEFAULT_EVENTS_MARGIN_S))
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

    def expected_for(self, label: str) -> int | None:
        """The number typed for a scan, else its median or study default (None if neither)."""
        automatic = self.automatic.get(label)
        return self.spins[label].value() or (automatic[0] if automatic else None)

    def too_long(self, label: str) -> list[DataFile]:
        expected = self.expected_for(label)
        if not expected:
            return []
        return [d for d in self.scans[label] if d.volumes is not None and d.volumes > expected]

    def update_from_logs(self, row: int, label: str) -> None:
        """The 'From logs' cell: the suggested volumes at the margin set for the scan."""
        ends = self.event_ends.get(label, [])
        margin = self.margins[label].value() if label in self.margins else 0.0
        suggested = volumes_from_logs(ends, margin)
        if suggested is None:
            item = QTableWidgetItem("-")
            item.setToolTip(
                "No complete behaviour log for this scan (Behaviour logs tab), and no "
                "events.tsv next to its runs."
            )
        else:
            volumes, longest = suggested
            source = (
                "complete behaviour logs"
                if label in self.log_ends
                else "events.tsv files in BIDS (the Behaviour logs tab hasn't read the logs)"
            )
            item = QTableWidgetItem(str(volumes))
            item.setToolTip(
                f"Median over {len(ends)} {source}: last event + {margin:g} s, in volumes at "
                f"the scan's TR. Longest: {longest}."
            )
        self.table.setItem(row, self.FROM_LOGS, item)

    def on_margin_changed(self, row: int, label: str) -> None:
        self.update_from_logs(row, label)
        self.saved_label.setText("Saving...")
        self.autosave.start()

    def on_changed(self, row: int, label: str) -> None:
        self.update_row(row, label)
        self.saved_label.setText("Saving...")
        self.autosave.start()  # restarts while typing: saves once typing pauses

    def update_row(self, row: int, label: str) -> None:
        volumes = self.expected_for(label)
        tr = next((d.repetition_time for d in self.scans[label] if d.repetition_time), None)
        minutes = f"{volumes * tr / 60:.1f}" if tr and volumes else ""
        self.table.setItem(row, self.MINUTES, QTableWidgetItem(minutes))
        long_runs = self.too_long(label)
        item = QTableWidgetItem(str(len(long_runs)) if long_runs else "")
        item.setToolTip("\n".join(f"{d.path.name}: {d.volumes} volumes" for d in long_runs))
        item.setForeground(QColor(LONG_COLOR))
        self.table.setItem(row, self.TOO_LONG, item)
        if len(self.spins) == len(self.scans):  # not while the rows are still being built
            self.update_trim_button()

    def update_trim_button(self) -> None:
        count = sum(len(self.too_long(label)) for label in self.scans)
        self.trim_button.setText(f"Trim {count} long run(s)...")
        self.trim_button.setEnabled(self.can_trim and count > 0)
        if not self.can_trim:
            self.trim_button.setToolTip("Can't trim now: another job is changing the BIDS folder.")

    def expected(self) -> dict[str, int]:
        """Only numbers the human set: a scan left at "median" keeps following its runs."""
        return {label: spin.value() for label, spin in self.spins.items() if spin.value()}

    def save(self) -> None:
        self.autosave.stop()
        # Keep values for scans not in the folder right now (e.g. all runs deleted).
        kept = {k: v for k, v in read_expected(self.bids_root).items() if k not in self.spins}
        write_expected(self.bids_root, kept | self.expected())
        kept_margins = {
            k: v for k, v in read_events_margins(self.bids_root).items() if k not in self.margins
        }
        margins = {label: spin.value() for label, spin in self.margins.items()}
        write_events_margins(self.bids_root, kept_margins | margins)
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
            self.banner.set_messages(
                ["No BIDS folder chosen: choose it on the Setup & Convert to BIDS tab."]
            )
            return
        self.panel = ScanLengthsPanel(
            bids_root, data_files, self, log_ends=log_event_ends(self.log_infos, data_files)
        )
        self.banner.set_messages(
            []
            if self.panel.scans
            else [
                f"No BOLD scans in {bids_root} yet: convert the DICOMs on the Setup & Convert "
                "to BIDS tab first."
            ]
        )
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
