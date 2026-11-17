"""A Docker image a tab needs, managed from inside the toolbox: C-PAC on Preprocess, MRIQC on
Scan quality.

* Not on this computer: a green 'Download ...' button, then a progress bar with how much is
  downloaded (see processing/docker_pull.py for how progress is measured).
* A download that didn't finish (failed, or the app closed mid-way; remembered across
  restarts): a green 'Continue download (n% done)' and a red 'Remove partial download'.
* On this computer: 'Remove ... image' (after asking), to free its disk space; locked while
  the tab is running it.
* Docker not running (unknown): nothing shown, the tab's own warning says why.
"""

import re
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QWidget,
)

from fmri_local_process.gui.qt_common import (
    ERROR_COLOR,
    PROCESS_ACCENT_COLOR,
    RAW_ACCENT_COLOR,
    primary_action_stylesheet,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.processing.docker_pull import (
    PullProgress,
    pull_image,
    remove_image,
    remove_partial,
)

PROGRESS_STEPS = 1000  # the bar's resolution
DOWNLOAD_COLOR = RAW_ACCENT_COLOR  # green: something to get
DOWNLOAD_HOVER_COLOR = "#1e8449"
REMOVE_HOVER_COLOR = "#c0392b"  # over ERROR_COLOR: giving up on a partial download


def pull_log(image: str) -> Path:
    return Path(tempfile.gettempdir()) / f"fmri_docker_pull_{re.sub(r'[^\w.-]', '_', image)}.txt"


class PullThread(QThread):
    progress = Signal(float, str)  # 0..1 (-1: not measurable yet), what is done
    done = Signal(str)  # why it failed, "" when it worked

    def __init__(self, image: str) -> None:
        super().__init__()
        self.image = image

    def run(self) -> None:
        exe = shutil.which("docker")
        if exe is None:
            self.done.emit("Docker isn't installed.")
            return
        log_path = pull_log(self.image)
        try:
            code = pull_image(Path(exe), self.image, log_path, self.relay)
        except OSError as e:
            self.done.emit(str(e))
            return
        if code == 0:
            self.done.emit("")
            return
        self.done.emit(
            f"{last_line(log_path) or f'docker pull exit {code}'}. Check the internet "
            "connection and click Download again: the parts already downloaded are kept."
        )

    def relay(self, progress: PullProgress) -> None:
        if not progress.stages:  # docker hasn't reported a layer yet: no measure to show
            self.progress.emit(-1.0, "Starting the download...")
        else:
            self.progress.emit(progress.fraction(), progress.text())


class RemoveThread(QThread):
    done = Signal(str)  # why it failed, "" when removed

    def __init__(self, image: str, partial: bool = False) -> None:
        super().__init__()
        self.image, self.partial = image, partial

    def run(self) -> None:
        exe = shutil.which("docker")
        if exe is None:
            self.done.emit("Docker isn't installed.")
            return
        remove = remove_partial if self.partial else remove_image
        self.done.emit(remove(Path(exe), self.image) or "")


def last_line(log_path: Path) -> str:
    """docker's last words in a pull log (its error, when it failed)."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    text = next((line.strip() for line in reversed(lines) if line.strip()), "")
    return text if len(text) <= 300 else text[:300] + "..."


class ImageDownload(QWidget):
    """[Download <what> (<size>)]  [=====     42%]  1.9 of 5.0 GB downloaded, ...
    or, once it is here:  [Remove <what>]"""

    finished = Signal(str)  # a download ended: why it failed, "" when the image is here now
    removed = Signal(str)  # a removal ended: why it failed, "" when it is gone

    def __init__(
        self,
        image: str,
        what: str,
        size_text: str,
        remove_note: str = "",
        settings: QSettings | None = None,
    ) -> None:
        super().__init__()
        self.image, self.what, self.size_text = image, what, size_text
        self.remove_note = remove_note  # added to the 'Remove?' question
        # Where an unfinished download is remembered across restarts (None: this session only).
        self.settings = settings
        self.partial = str(settings.value(self.partial_key, "") or "") if settings else ""
        self.present: bool | None = None
        self.lock_reason = ""
        self.thread_: PullThread | None = None
        self.remover: RemoveThread | None = None
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.button = QPushButton()
        self.button.setStyleSheet(primary_action_stylesheet(DOWNLOAD_COLOR, DOWNLOAD_HOVER_COLOR))
        self.button.setToolTip(
            wrap_tooltip(
                f"docker pull {image}: once per computer. Keep the PC awake and online; if the "
                "download stops, click again and it picks up where it was."
            )
        )
        self.button.clicked.connect(self.start)
        row.addWidget(self.button)
        self.remove_partial_button = QPushButton("Remove partial download")
        self.remove_partial_button.setStyleSheet(
            primary_action_stylesheet(ERROR_COLOR, REMOVE_HOVER_COLOR)
        )
        self.remove_partial_button.setToolTip(
            wrap_tooltip(
                "Give up on this download: clear what it left on disk (and other unused, "
                "untagged Docker images). Downloading later starts from the beginning."
            )
        )
        self.remove_partial_button.clicked.connect(self.on_remove_partial)
        row.addWidget(self.remove_partial_button)
        self.remove_button = QPushButton(f"Remove {what}")
        self.remove_button.clicked.connect(self.on_remove)
        row.addWidget(self.remove_button)
        self.bar = QProgressBar()
        self.bar.setRange(0, PROGRESS_STEPS)
        self.bar.setFormat(f"Downloading {what}: %p%")
        self.bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {PROCESS_ACCENT_COLOR}; }}"
            "QProgressBar { text-align: center; }"
        )
        self.bar.setVisible(False)
        row.addWidget(self.bar, 2)
        self.label = QLabel()
        style_secondary_label(self.label)
        self.label.setVisible(False)
        row.addWidget(self.label, 1)
        row.addStretch(0)
        self.update_buttons()

    @property
    def busy(self) -> bool:
        return self.thread_ is not None or self.remover is not None

    @property
    def partial_key(self) -> str:
        return f"partial_downloads/{re.sub(r'[^\w.-]', '_', self.image)}"

    def set_partial(self, text: str) -> None:
        """Remember how far an unfinished download got ('' when none is unfinished)."""
        self.partial = text
        if self.settings is not None:
            if text:
                self.settings.setValue(self.partial_key, text)
            else:
                self.settings.remove(self.partial_key)

    def set_present(self, present: bool | None) -> None:
        """Whether the image is on this computer (None: unknown, Docker not running)."""
        self.present = present
        if present and self.partial and not self.busy:  # finished some other way
            self.set_partial("")
        self.update_buttons()

    def set_locked(self, reason: str) -> None:
        """Why the image can't be removed now ('' if it can), e.g. it is running."""
        self.lock_reason = reason
        self.update_buttons()

    def update_buttons(self) -> None:
        downloading = self.thread_ is not None
        self.setVisible(self.present is not None or self.busy)
        self.button.setVisible(self.present is False or downloading)
        self.button.setEnabled(not self.busy)
        self.button.setText(
            f"Continue download ({self.partial})"
            if self.partial and not downloading
            else f"Download {self.what} ({self.size_text})"
        )
        self.remove_partial_button.setVisible(
            self.present is False and bool(self.partial) and not downloading
        )
        self.remove_partial_button.setEnabled(not self.busy)
        self.remove_button.setVisible(self.present is True and not downloading)
        self.remove_button.setEnabled(not self.busy and not self.lock_reason)
        self.remove_button.setToolTip(
            f"Can't remove it now: {self.lock_reason}."
            if self.lock_reason
            else wrap_tooltip(
                f"docker rmi {self.image}: frees its disk space ({self.size_text}). It has to be "
                "downloaded again before it can be used."
            )
        )

    # ---- downloading ---------------------------------------------------------------------------

    def start(self) -> None:
        if self.busy:
            return
        self.bar.setRange(0, PROGRESS_STEPS)
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.label.setText("Asking the registry how big it is...")
        self.label.setVisible(True)
        if not self.partial:  # remembered until it finishes: closing mid-way leaves it partial
            self.set_partial("0% done")
        self.thread_ = PullThread(self.image)
        self.thread_.progress.connect(self.on_progress)
        self.thread_.done.connect(self.on_done)
        self.thread_.start()
        self.update_buttons()

    def on_progress(self, fraction: float, text: str) -> None:
        if fraction < 0:  # nothing to measure yet: Qt's moving 'busy' bar
            self.bar.setRange(0, 0)
            self.label.setText(text)
            return
        self.bar.setRange(0, PROGRESS_STEPS)
        self.bar.setValue(round(fraction * PROGRESS_STEPS))
        self.label.setText(text)
        if fraction > 0:
            self.set_partial(f"{fraction:.0%} done")

    def on_done(self, error: str) -> None:
        if self.thread_ is not None:
            self.thread_.wait()
        self.thread_ = None
        self.bar.setRange(0, PROGRESS_STEPS)
        if error:
            self.label.setText(f"⚠ Download stopped: {error}")
        else:
            self.bar.setValue(PROGRESS_STEPS)
            self.bar.setVisible(False)
            self.label.setText(f"{self.what} downloaded.")
            self.present = True
            self.set_partial("")
        self.update_buttons()
        self.finished.emit(error)

    # ---- removing ------------------------------------------------------------------------------

    def on_remove(self) -> None:
        if self.busy or self.lock_reason:
            return
        note = f"\n\n{self.remove_note}" if self.remove_note else ""
        answer = QMessageBox.question(
            self,
            f"Remove {self.what}?",
            f"Remove {self.image} from this computer, freeing its disk space "
            f"({self.size_text})?\n\nIt has to be downloaded again before it can be used. "
            f"Your data and results aren't touched.{note}",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.start_removing(partial=False)

    def on_remove_partial(self) -> None:
        if self.busy:
            return
        answer = QMessageBox.question(
            self,
            "Remove the partial download?",
            f"Give up on the unfinished download of {self.image} ({self.partial}) and clear "
            "what it left on disk?\n\nThis also removes other unused, untagged Docker images "
            "(old versions nothing refers to). Your data and results aren't touched. "
            "Downloading later starts from the beginning.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.start_removing(partial=True)

    def start_removing(self, partial: bool) -> None:
        self.label.setText(
            "Clearing the partial download..." if partial else f"Removing {self.what}..."
        )
        self.label.setVisible(True)
        self.bar.setVisible(False)
        self.remover = RemoveThread(self.image, partial)
        self.remover.done.connect(self.on_removed)
        self.remover.start()
        self.update_buttons()

    def on_removed(self, error: str) -> None:
        if self.remover is not None:
            self.remover.wait()
        self.remover = None
        if error:
            self.label.setText(f"⚠ Not removed: {error}")
        else:
            self.label.setText(f"{self.what} removed.")
            self.present = False
            self.set_partial("")
        self.update_buttons()
        self.removed.emit(error)

    def wait(self) -> None:
        """Closing the window: a running QThread can't be destroyed. The pull itself runs on
        in docker if the app exits, and a later Download resumes it."""
        for thread in (self.thread_, self.remover):
            if thread is not None:
                thread.wait()
