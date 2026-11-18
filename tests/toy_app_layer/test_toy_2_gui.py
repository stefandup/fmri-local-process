"""Step 2: the same window as step 0, now drawing on the app layer (toy_gui.py).

Compare with test_toy_0_plain_gui.py: the clicks and the checks are the same. What changed
is where things live: the folder and busy flag are in window.state (a toy_app.ToolboxState),
and each tab is its own page object, in window.pages.

See test_toy_0_plain_gui.py for how to read a Qt test (tmp_path, offscreen, click/read back).
"""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # must be set before Qt starts

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from toy_app import COUNT_TAB, FOLDER_TAB, ToolboxState  # noqa: E402
from toy_data import make_folder  # noqa: E402
from toy_gui import CountPage, FolderPage, ToolboxWindow  # noqa: E402


def open_window(settings_file: Path) -> ToolboxWindow:
    """Build the window as main() does, but with settings in a throwaway .ini file instead
    of the registry. QSettings has the value()/setValue() methods ToolboxState asks for."""
    _app = QApplication.instance() or QApplication([])
    settings = QSettings(str(settings_file), QSettings.Format.IniFormat)
    return ToolboxWindow(ToolboxState(settings))


def test_choosing_a_folder_updates_the_header(tmp_path: Path) -> None:
    folder = make_folder(tmp_path)
    window = open_window(tmp_path / "settings.ini")
    page = window.pages[FOLDER_TAB]
    assert isinstance(page, FolderPage)  # tells pyright which page class this is

    page.path_edit.setText(str(folder))
    page.use_button.click()  # -> state.set_folder -> folder_changed -> update_header

    assert window.header.text() == "notes"


def test_counting_runs_on_a_thread_and_fills_the_table(tmp_path: Path) -> None:
    window = open_window(tmp_path / "settings.ini")
    window.state.set_folder(make_folder(tmp_path))  # skip tab 1: set the state directly
    page = window.pages[COUNT_TAB]
    assert isinstance(page, CountPage)

    page.run_button.click()
    assert window.header.text() == "notes  ⏳ counting"

    assert page.counter is not None
    page.counter.wait()  # 1. the worker thread has finished...
    QApplication.processEvents()  # 2. ...now deliver its queued signals to the page

    assert page.table.rowCount() == 2
    assert page.status.text() == "2 files, 5 lines"
    assert window.header.text() == "notes"  # no longer busy
