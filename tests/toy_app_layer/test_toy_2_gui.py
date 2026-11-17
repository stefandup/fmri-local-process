"""Step 2: the PySide6 window on the same app layer, offscreen (no window pops up)."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402 -- needs QT_QPA_PLATFORM set first
from PySide6.QtWidgets import QApplication  # noqa: E402
from test_toy_1_app import make_folder  # noqa: E402
from toy_app import COUNT_TAB, FOLDER_TAB, ToolboxState  # noqa: E402
from toy_gui import CountPage, FolderPage, ToolboxWindow  # noqa: E402


def make_window(tmp_path: Path) -> ToolboxWindow:
    _app = QApplication.instance() or QApplication([])
    # The GUI hands the state a QSettings, which fits toy_app.Settings as is. (The real
    # toolbox uses the registry; a test uses an .ini file in a temp folder.)
    settings = QSettings(str(tmp_path / "toy.ini"), QSettings.Format.IniFormat)
    return ToolboxWindow(ToolboxState(settings))


def test_choosing_a_folder_updates_the_header(tmp_path: Path) -> None:
    window = make_window(tmp_path)
    page = window.pages[FOLDER_TAB]
    assert isinstance(page, FolderPage)

    page.path_edit.setText(str(make_folder(tmp_path)))
    page.use_button.click()  # -> state.set_folder -> folder_changed -> update_header

    assert window.header.text() == "notes"


def test_counting_runs_on_a_thread_and_fills_the_table(tmp_path: Path) -> None:
    window = make_window(tmp_path)
    window.state.set_folder(make_folder(tmp_path))
    page = window.pages[COUNT_TAB]
    assert isinstance(page, CountPage)

    page.run_button.click()
    assert "counting" in window.header.text()  # busy while it runs
    assert page.counter is not None
    page.counter.wait()  # the worker thread is done...
    QApplication.processEvents()  # ...and now its queued signals reach the GUI thread

    assert page.table.rowCount() == 2
    assert page.status.text() == "2 files, 5 lines"
    assert window.header.text() == "notes"  # no longer busy
