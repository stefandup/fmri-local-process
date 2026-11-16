"""Trimming long BOLD runs, shared by the Crosscheck and Scan lengths tabs: one at a time,
on a worker thread (each run is a few seconds of gzip), after the human confirms."""

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from fmri_local_process.gui.toolbox_state import TRIMMING, ToolboxState
from fmri_local_process.processing.crosscheck import CrosscheckError, DataFile, trim_run

MAX_LISTED = 15


class TrimThread(QThread):
    """Trims runs one by one; reports progress as log lines."""

    line = Signal(str)

    def __init__(self, bids_root: Path, jobs: list[tuple[DataFile, int]]):
        super().__init__()
        self.bids_root, self.jobs = bids_root, jobs

    def run(self) -> None:
        for number, (data, volumes) in enumerate(self.jobs, start=1):
            try:
                trim_run(self.bids_root, data, volumes)
            except (CrosscheckError, OSError, ValueError) as e:
                self.line.emit(f"NOT TRIMMED  {e}")
                continue
            self.line.emit(
                f"Trimmed {data.path.name} to {volumes} volumes ({number}/{len(self.jobs)})"
            )


def confirm_text(jobs: list[tuple[DataFile, int]]) -> str:
    names = "\n".join(f"  {d.path.name}: {d.volumes} -> {n}" for d, n in jobs[:MAX_LISTED])
    more = f"\n  ... and {len(jobs) - MAX_LISTED} more" if len(jobs) > MAX_LISTED else ""
    return (
        f"Trim {len(jobs)} run(s), keeping the first volumes?\n{names}{more}\n\n"
        "The cut volumes are deleted from the BIDS folder (the raw DICOMs keep them: "
        "delete the file and convert again to restore it)."
    )


class Trimmer(QObject):
    line = Signal(str)  # progress, for an activity pane

    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        self.worker: TrimThread | None = None

    @property
    def running(self) -> bool:
        return self.worker is not None

    def start(self, parent: QWidget, jobs: list[tuple[DataFile, int]]) -> None:
        bids_root = self.state.folder("bids")
        if not jobs or self.worker is not None or bids_root is None:
            return
        if reason := self.state.lock_reason():
            QMessageBox.information(parent, "Not now", f"Can't trim now: {reason}.")
            return
        answer = QMessageBox.question(parent, "Confirm", confirm_text(jobs))
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.worker = TrimThread(bids_root, jobs)
        self.worker.line.connect(self.line)
        self.worker.finished.connect(self._done)
        self.state.set_busy(TRIMMING, True)
        self.worker.start()

    def _done(self) -> None:
        if self.worker is not None:
            self.worker.wait()
        self.worker = None
        self.state.set_busy(TRIMMING, False)
        self.state.bump()
