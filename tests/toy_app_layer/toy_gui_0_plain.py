"""Step 0: the toy as an ordinary PySide6 app, built the way gui/ is built today.

Everything lives in Qt: the window keeps the folder and the busy flag, the counting loop sits
inside a QThread, and the folder is remembered with QSettings (on Windows, in the registry).
There is no app layer yet. toy_gui.py (step 2) is the same window after the split.

Run it:  python tests/toy_app_layer/toy_gui_0_plain.py

The parts of any Qt app, top to bottom in this file:
1. a QThread that does slow work off the GUI thread and reports back through Signals
2. a QMainWindow that builds the widgets and connects button clicks to methods ("slots")
3. main(): create the QApplication, show the window, run the event loop until it closes
"""

import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from toy_data import make_folder
from toy_processing import count_lines, text_files  # SDP we import the processing layer

# ---- 1. The worker thread -------------------------------------------------------------------


class CountThread(QThread):
    """Counts lines on a worker thread, so the window stays responsive meanwhile.

    Widgets may only be changed on the GUI thread. So run() never touches a widget: it emits
    a signal, and Qt delivers it to the connected slot on the GUI thread."""

    file_counted = Signal(str, int, int, int)  # name, lines, done, total
    all_done = Signal(int, int)  # files, lines

    def __init__(self, folder: Path, pause: float = 0.0) -> None:
        super().__init__()
        self.folder = folder
        self.pause = pause  # seconds to wait after each file, so you can watch it happen

    def run(self) -> None:  # thread.start() calls this, on the worker thread
        files = text_files(self.folder)
        total_lines = 0
        for done, path in enumerate(files, start=1):
            lines = count_lines(path)
            total_lines += lines
            self.file_counted.emit(path.name, lines, done, len(files))
            time.sleep(self.pause)
        self.all_done.emit(len(files), total_lines)


# ---- 2. The window --------------------------------------------------------------------------


class PlainWindow(QMainWindow):
    """A header and two tabs. The window itself holds the state: the folder and busy flag."""

    def __init__(self, settings: QSettings, pause: float = 0.0) -> None:
        super().__init__()
        self.setWindowTitle("Toy toolbox (plain Qt)")
        self.settings = settings
        self.pause = pause
        saved = str(settings.value("folder", "") or "")  # last session's folder, if any
        self.folder: Path | None = Path(saved) if saved else None
        self.busy = False
        self.counter: CountThread | None = None

        self.header = QLabel()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_folder_tab(), "1. Folder")
        self.tabs.addTab(self.build_count_tab(), "2. Line counts")

        central = QWidget()  # a QMainWindow shows one central widget; it holds the rest
        layout = QVBoxLayout(central)
        layout.addWidget(self.header)
        layout.addWidget(self.tabs)
        self.setCentralWidget(central)
        self.update_header()

    # -- tab 1 --

    def build_folder_tab(self) -> QWidget:
        page = QWidget()
        self.path_edit = QLineEdit(str(self.folder or ""))
        self.use_button = QPushButton("Use this folder")
        self.use_button.clicked.connect(self.use_folder)  # a click calls use_folder()
        layout = QVBoxLayout(page)
        layout.addWidget(self.path_edit)
        layout.addWidget(self.use_button)
        layout.addStretch()
        return page

    def use_folder(self) -> None:
        self.folder = Path(self.path_edit.text())
        self.settings.setValue("folder", str(self.folder))  # remembered for next time
        self.update_header()

    # -- tab 2 --

    def build_count_tab(self) -> QWidget:
        page = QWidget()
        self.run_button = QPushButton("Count lines")
        self.run_button.clicked.connect(self.start_counting)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["File", "Lines"])
        self.status = QLabel()
        layout = QVBoxLayout(page)
        for widget in (self.run_button, self.table, self.status):
            layout.addWidget(widget)
        return page

    def start_counting(self) -> None:
        if self.folder is None or self.busy:
            self.status.setText("Can't start: " + ("counting" if self.busy else "no folder"))
            return
        self.table.setRowCount(0)
        self.counter = CountThread(self.folder, self.pause)
        self.counter.file_counted.connect(self.add_row)  # these slots run on the GUI thread
        self.counter.all_done.connect(self.finish)
        self.busy = True
        self.update_header()
        self.counter.start()  # returns at once; run() goes on in the background

    def add_row(self, name: str, lines: int, done: int, total: int) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, QTableWidgetItem(str(lines)))
        self.status.setText(f"{done} of {total}")

    def finish(self, files: int, lines: int) -> None:
        self.busy = False
        self.update_header()
        self.status.setText(f"{files} files, {lines} lines")

    # -- header --

    def update_header(self) -> None:
        name = self.folder.name if self.folder else "No folder chosen"
        self.header.setText(f"{name}  ⏳ counting" if self.busy else name)


# ---- 3. Start the app -----------------------------------------------------------------------


def main() -> None:
    app = QApplication(sys.argv)  # exactly one per program, before any widget
    # Organisation + app name: on Windows this is the registry key
    # HKEY_CURRENT_USER\Software\MooiToolbox\ToyPlain (look in regedit after a run).
    settings = QSettings("MooiToolbox", "ToyPlain")
    window = PlainWindow(settings, pause=1.0)  # 1 s per file, so you can watch the thread
    if window.folder is None:  # first run: offer a demo folder in the temp directory
        window.path_edit.setText(str(make_folder(Path(tempfile.gettempdir()) / "toy_demo")))
    window.resize(500, 350)
    window.show()
    sys.exit(app.exec())  # the event loop: waits for clicks, runs slots, until the window closes


if __name__ == "__main__":
    main()
