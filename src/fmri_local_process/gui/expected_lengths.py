"""Expected scan lengths: the human sets how many volumes each BOLD scan should have.

Saved in the BIDS folder (code/expected_volumes.json) so it travels with the data. The
crosscheck then flags runs shorter than that, and offers to trim runs that are longer
(scans stopped by hand after the task).
"""

from collections import Counter
from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.processing.crosscheck import (
    EXPECTED_FILE,
    DataFile,
    read_expected,
    write_expected,
)

MAX_VOLUMES = 100_000


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


class ExpectedLengthsDialog(QDialog):
    COLUMNS = ("Scan", "Runs", "Volumes found", "Expected volumes", "Minutes")

    def __init__(self, bids_root: Path, data_files: list[DataFile], parent: QWidget | None):
        super().__init__(parent)
        self.setWindowTitle("Expected scan lengths")
        self.resize(900, 450)
        self.bids_root = bids_root
        self.scans = scan_lengths(data_files)
        expected = read_expected(bids_root)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Set how many volumes each scan should have (0: not set, compare with the other "
            "sessions instead). Runs with fewer are flagged as short; runs with more can be "
            "trimmed to this length, keeping the first volumes.\n"
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
            spin.setSpecialValueText("not set")
            spin.setValue(expected.get(label, 0))
            tr = next((d.repetition_time for d in runs if d.repetition_time), None)
            spin.valueChanged.connect(lambda _=0, r=row, s=spin, t=tr: self.show_minutes(r, s, t))
            self.table.setCellWidget(row, 3, spin)
            self.spins[label] = spin
            self.show_minutes(row, spin, tr)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        if not self.scans:
            layout.addWidget(QLabel("No BOLD scans found in this BIDS folder yet."))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def show_minutes(self, row: int, spin: QSpinBox, tr: float | None) -> None:
        text = f"{spin.value() * tr / 60:.1f}" if tr and spin.value() else ""
        self.table.setItem(row, 4, QTableWidgetItem(text))

    def expected(self) -> dict[str, int]:
        return {label: spin.value() for label, spin in self.spins.items() if spin.value()}

    def save(self) -> None:
        # Keep values for scans not in the folder right now (e.g. all runs deleted).
        kept = {k: v for k, v in read_expected(self.bids_root).items() if k not in self.spins}
        write_expected(self.bids_root, kept | self.expected())
        self.accept()
