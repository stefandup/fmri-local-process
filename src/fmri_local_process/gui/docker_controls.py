"""Docker's state on a tab that runs Docker (Preprocess: C-PAC, Scan quality: MRIQC), with
Start / Restart buttons:

    ✓ Docker is running.                                  [Start Docker] [Restart Docker]

The owning tab asks Docker (on its own thread, with what else it needs to know) and calls
show(); a start / restart runs here, and `action_done` tells the tab to ask again. Tabs
re-ask every DOCKER_RECHECK_MS, so the line follows Docker started or stopped elsewhere.
"""

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from fmri_local_process.app.docker import docker_line
from fmri_local_process.gui.qt_common import style_secondary_label, wrap_tooltip
from fmri_local_process.processing.cpac_docker import docker_desktop


class DockerControlThread(QThread):
    """Starts / restarts Docker Desktop off the GUI thread (takes up to a minute or two)."""

    done = Signal(str, str)  # action, error ("" when it worked)

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action

    def run(self) -> None:
        self.done.emit(self.action, docker_desktop(self.action) or "")


class DockerStatus(QWidget):
    action_started = Signal(str)  # action
    action_done = Signal(str, str)  # action, error ("" when it worked)

    def __init__(self, restart_note: str = "") -> None:
        super().__init__()
        self.control: DockerControlThread | None = None
        self.shown: tuple[bool | None, str, str] = (None, "", "")  # running, error, warning
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel("Checking Docker...")
        self.label.setWordWrap(True)
        style_secondary_label(self.label)
        row.addWidget(self.label, 1)
        self.start_button = QPushButton("Start Docker")
        self.start_button.setToolTip("Runs 'docker desktop start'.")
        self.start_button.clicked.connect(lambda: self.run("start"))
        row.addWidget(self.start_button)
        self.restart_button = QPushButton("Restart Docker")
        self.restart_button.setToolTip(
            wrap_tooltip(
                f"Runs 'docker desktop restart'. Stops any running container. {restart_note}"
            )
        )
        self.restart_button.clicked.connect(lambda: self.run("restart"))
        row.addWidget(self.restart_button)
        self.show_state(None)

    @property
    def busy(self) -> bool:
        return self.control is not None

    def show_state(self, running: bool | None, error: str = "", warning: str = "") -> None:
        """running None: not known yet; False: why not (error); True: fine, or a warning
        (e.g. an image the tab needs isn't downloaded)."""
        self.shown = (running, error, warning)
        action = self.control.action if self.control is not None else ""
        text, color = docker_line(running, error, warning, action)
        self.label.setText(text)
        if color:
            self.label.setStyleSheet(f"color: {color};")
        else:
            style_secondary_label(self.label)
        self.start_button.setEnabled(not self.busy and running is False)
        self.restart_button.setEnabled(not self.busy)

    def run(self, action: str) -> None:
        if self.control is not None:
            return
        self.control = DockerControlThread(action)
        self.control.done.connect(self.on_done)
        self.show_state(*self.shown)
        self.control.start()
        self.action_started.emit(action)

    def on_done(self, action: str, error: str) -> None:
        if self.control is not None:
            self.control.wait()
        self.control = None
        self.show_state(*self.shown)
        self.action_done.emit(action, error)

    def wait(self) -> None:
        """Closing the window: a running QThread can't be destroyed."""
        if self.control is not None:
            self.control.wait()
