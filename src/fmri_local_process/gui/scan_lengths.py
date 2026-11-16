"""Scan lengths & trimming: how many volumes each BOLD scan should have, and trimming the
runs that are longer (scans stopped by hand after the task).

The expected length is by default the (upper) median of all a scan's runs; the human can set
another number, which is saved in the BIDS folder (code/expected_volumes.json) so it travels
with the data. The crosscheck flags runs shorter than that as short.
"""

from collections import Counter
from pathlib import Path

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.processing.crosscheck import (
    EXPECTED_FILE,
    DataFile,
    median_volumes,
    read_expected,
    write_expected,
)

MAX_VOLUMES = 100_000
LONG_COLOR = "#3498db"  # the crosscheck's colour for "long"


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


class ScanLengthsDialog(QDialog):
    COLUMNS = ("Scan", "Runs", "Volumes found", "Expected volumes", "Minutes", "Too long")
    EXPECTED, MINUTES, TOO_LONG = 3, 4, 5

    def __init__(
        self,
        bids_root: Path,
        data_files: list[DataFile],
        parent: QWidget | None,
        can_trim: bool = True,
    ):
        super().__init__(parent)
        self.setWindowTitle("Scan lengths & trimming")
        self.resize(1000, 480)
        self.bids_root = bids_root
        self.scans = scan_lengths(data_files)
        self.medians = median_volumes(data_files)
        self.trim_requested = False
        expected = read_expected(bids_root)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "How many volumes each scan should have. By default the median of its runs; type "
            "another number to set it (remembered), or step down to the bottom to go back to "
            "the median. Runs with fewer volumes are flagged as short. Runs with more can be "
            "trimmed to this length, keeping the first volumes: the cut volumes are deleted "
            "from the BIDS folder (the raw DICOMs keep them; delete the file and convert "
            "again to undo).\n"
            f"Saved in {bids_root / EXPECTED_FILE}."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(self.scans), len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.spins: dict[str, QSpinBox] = {}
        for row, (label, runs) in enumerate(self.scans.items()):
            cells = (label, str(len(runs)), length_note(runs))
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(text))
            spin = QSpinBox()
            spin.setRange(0, MAX_VOLUMES)
            median = self.medians.get(label)
            spin.setSpecialValueText(f"median ({median})" if median else "not set")
            spin.setValue(expected.get(label, 0))
            spin.valueChanged.connect(lambda _=0, r=row, lab=label: self.update_row(r, lab))
            self.table.setCellWidget(row, self.EXPECTED, spin)
            self.spins[label] = spin
            self.update_row(row, label)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        if not self.scans:
            layout.addWidget(QLabel("No BOLD scans found in this BIDS folder yet."))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.trim_button = QPushButton("Save and trim long runs...")
        self.trim_button.setToolTip(
            "Save these lengths, then cut every run that is longer down to its expected "
            "length, keeping the first volumes (you'll be asked to confirm)."
        )
        self.trim_button.clicked.connect(self.save_and_trim)
        buttons.addButton(self.trim_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.can_trim = can_trim
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.update_trim_button()

    def expected_for(self, label: str) -> int | None:
        """The number typed for a scan, else its median (None if neither)."""
        return self.spins[label].value() or self.medians.get(label)

    def too_long(self, label: str) -> list[DataFile]:
        expected = self.expected_for(label)
        if not expected:
            return []
        return [d for d in self.scans[label] if d.volumes is not None and d.volumes > expected]

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
        if hasattr(self, "trim_button"):
            self.update_trim_button()

    def update_trim_button(self) -> None:
        count = sum(len(self.too_long(label)) for label in self.scans)
        self.trim_button.setText(f"Save and trim {count} long run(s)...")
        self.trim_button.setEnabled(self.can_trim and count > 0)
        if not self.can_trim:
            self.trim_button.setToolTip("A trim is already running.")

    def expected(self) -> dict[str, int]:
        """Only numbers the human set: a scan left at "median" keeps following its runs."""
        return {label: spin.value() for label, spin in self.spins.items() if spin.value()}

    def save(self) -> None:
        # Keep values for scans not in the folder right now (e.g. all runs deleted).
        kept = {k: v for k, v in read_expected(self.bids_root).items() if k not in self.spins}
        write_expected(self.bids_root, kept | self.expected())
        self.accept()

    def save_and_trim(self) -> None:
        """Save; the crosscheck window then trims (after confirming) from the saved lengths."""
        self.trim_requested = True
        self.save()
