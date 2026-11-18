"""Layer 3a, gui/: the PySide6 window. It only displays: widgets, plus a thin QThread that
relays a job's listener calls as Qt signals.

BEFORE (how gui/ is built today): the loop sits inside the QThread, so only Qt can run it.

    class CountThread(QThread):
        file_counted = Signal(str, int)
        def run(self):
            for path in text_files(self.folder):              # <- the logic lives here
                self.file_counted.emit(path.name, count_lines(path))

AFTER (below): the loop lives in toy_app.CountJob, and the QThread just runs it.

Compare with toy_gui_0_plain.py, the same window before the split.
Run it:  python tests/toy_app_layer/toy_gui.py
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
from toy_app import (
    COUNT_TAB,
    COUNTING,
    FOLDER_TAB,
    TAB_ORDER,
    CountJob,
    CountSummary,
    ToolboxState,
    header_text,
    summary_text,
    tab_text,
)
from toy_data import make_folder

DEMO_PAUSE = 0.0  # main() sets 1 s per file, so you can watch the thread; tests keep 0


class CountThread(QThread):
    """Runs a CountJob off the GUI thread. It is the job's listener: each call arrives on the
    worker thread, and emitting a signal queues it to the GUI thread, where widgets may change."""

    file_counted = Signal(str, int, int, int)
    all_done = Signal(object)  # CountSummary

    def __init__(self, folder: Path) -> None:
        super().__init__()
        self.job = CountJob(folder, listener=self)

    def run(self) -> None:  # on the worker thread
        self.job.run()

    # The CountListener methods: just turn each call into a signal.
    def on_file(self, name: str, lines: int, done: int, total: int) -> None:
        self.file_counted.emit(name, lines, done, total)
        time.sleep(DEMO_PAUSE)  # on the worker thread, so the window stays responsive

    def on_done(self, summary: CountSummary) -> None:
        self.all_done.emit(summary)


class FolderPage(QWidget):
    """Tab 1: choose the folder (the real one uses a folder dialog)."""

    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        self.path_edit = QLineEdit(str(state.folder or ""))
        self.use_button = QPushButton("Use this folder")
        self.use_button.clicked.connect(lambda: state.set_folder(Path(self.path_edit.text())))
        layout = QVBoxLayout(self)
        layout.addWidget(self.path_edit)
        layout.addWidget(self.use_button)


class CountPage(QWidget):
    """Tab 2: run the job, show progress and the result."""

    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        self.counter: CountThread | None = None
        self.run_button = QPushButton("Count lines")
        self.run_button.clicked.connect(self.start)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["File", "Lines"])
        self.status = QLabel()
        layout = QVBoxLayout(self)
        for widget in (self.run_button, self.table, self.status):
            layout.addWidget(widget)

    def start(self) -> None:
        if self.state.folder is None or self.state.lock_reason():
            self.status.setText(f"Can't start: {self.state.lock_reason() or 'no folder'}")
            return
        self.table.setRowCount(0)
        self.counter = CountThread(self.state.folder)
        self.counter.file_counted.connect(self.add_row)  # these slots run on the GUI thread
        self.counter.all_done.connect(self.finish)
        self.state.set_busy(COUNTING, True)  # other tabs lock their changing actions
        self.counter.start()

    def add_row(self, name: str, lines: int, done: int, total: int) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, QTableWidgetItem(str(lines)))
        self.status.setText(f"{done} of {total}")

    def finish(self, summary: CountSummary) -> None:
        self.state.set_busy(COUNTING, False)
        self.status.setText(summary_text(summary))


PAGES = {FOLDER_TAB: FolderPage, COUNT_TAB: CountPage}  # tab name -> page class


class ToolboxWindow(QMainWindow):
    """The header and the tabs, built from the shared TAB_ORDER."""

    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        self.header = QLabel()
        self.tabs = QTabWidget()
        self.pages = {name: PAGES[name](state) for name in TAB_ORDER}
        for number, name in enumerate(TAB_ORDER, start=1):
            self.tabs.addTab(self.pages[name], tab_text(number, name))
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.header)
        layout.addWidget(self.tabs)
        self.setCentralWidget(central)
        # Plain-Python events from the app layer, connected exactly like Qt signals.
        state.folder_changed.connect(self.update_header)
        state.busy_changed.connect(self.update_header)
        self.update_header()

    def update_header(self) -> None:
        self.header.setText(header_text(self.state))


def main() -> None:
    global DEMO_PAUSE
    DEMO_PAUSE = 1.0
    app = QApplication(sys.argv)
    # Registry key HKEY_CURRENT_USER\Software\MooiToolbox\ToySplit (separate from step 0's).
    state = ToolboxState(QSettings("MooiToolbox", "ToySplit"))
    window = ToolboxWindow(state)
    page = window.pages[FOLDER_TAB]
    if state.folder is None and isinstance(page, FolderPage):  # first run: offer a demo folder
        page.path_edit.setText(str(make_folder(Path(tempfile.gettempdir()) / "toy_demo")))
    window.setWindowTitle("Toy toolbox (app layer)")
    window.resize(500, 350)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
