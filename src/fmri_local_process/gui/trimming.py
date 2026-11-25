"""Trimming long BOLD runs, shared by the Crosscheck and Scan lengths tabs: one at a time,
on a worker thread (each run is a few seconds of gzip), after the human confirms."""

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from fmri_local_process.app.state import TRIMMING
from fmri_local_process.app.trimming import TrimJob, confirm_text
from fmri_local_process.gui.toolbox_state import ToolboxState
from fmri_local_process.processing.crosscheck import DataFile


class TrimThread(QThread):
    """Trims runs one by one; reports progress as log lines."""

    line = Signal(str)

    def __init__(self, bids_root: Path, jobs: list[tuple[DataFile, int]]):
        super().__init__()
        self.job = TrimJob(bids_root, jobs, self.line.emit)

    def run(self) -> None:
        self.job.run()


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
