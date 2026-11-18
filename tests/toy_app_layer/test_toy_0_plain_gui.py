"""Step 0: test the plain Qt window (toy_gui_0_plain.py) the way a user would use it.

How to read a Qt test:
- `tmp_path` is a pytest built-in: name it as an argument and pytest hands each test its own
  new, empty folder (it cleans up old ones later). Nothing a test writes lands in the repo.
- QT_QPA_PLATFORM=offscreen makes Qt draw into memory, so no window pops up during tests.
- A test builds the window, presses its buttons (`button.click()`), and reads the widgets
  back (`label.text()`, `table.rowCount()`), just like checking the screen by eye.
"""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # must be set before Qt starts

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from toy_data import make_folder  # noqa: E402
from toy_gui_0_plain import PlainWindow  # noqa: E402


def open_window(settings_file: Path) -> PlainWindow:
    """Build the window as main() does, with one difference: main() keeps settings in the
    registry, which would overwrite your real saved folder. Here they go to settings_file,
    a plain-text .ini file. Same QSettings methods, so the window can't tell the difference."""
    _app = QApplication.instance() or QApplication([])  # Qt needs one before any widget
    settings = QSettings(str(settings_file), QSettings.Format.IniFormat)
    return PlainWindow(settings)


def test_choosing_a_folder_updates_the_header(tmp_path: Path) -> None:
    folder = make_folder(tmp_path)  # tmp_path/notes, with a.txt and b.txt
    window = open_window(tmp_path / "settings.ini")
    assert window.header.text() == "No folder chosen"

    window.path_edit.setText(str(folder))  # type the path...
    window.use_button.click()  # ...and press "Use this folder"

    assert window.header.text() == "notes"


def test_the_folder_is_remembered_next_time(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.ini"
    window = open_window(settings_file)
    window.path_edit.setText(str(make_folder(tmp_path)))
    window.use_button.click()

    # QSettings saves to disk a moment later, not on every setValue(); sync() saves now.
    window.settings.sync()
    # Look inside the settings file: it is plain text.
    assert "folder=" in settings_file.read_text()
    # A second window on the same file (= starting the app again) picks the folder up.
    assert open_window(settings_file).header.text() == "notes"


def test_counting_runs_on_a_thread_and_fills_the_table(tmp_path: Path) -> None:
    window = open_window(tmp_path / "settings.ini")
    window.path_edit.setText(str(make_folder(tmp_path)))
    window.use_button.click()

    window.run_button.click()  # starts the CountThread and returns at once
    assert window.header.text() == "notes  ⏳ counting"

    # In the real app the event loop keeps running. In a test we wait by hand:
    assert window.counter is not None
    window.counter.wait()  # 1. the worker thread has finished...
    QApplication.processEvents()  # 2. ...now deliver its queued signals to add_row / finish

    assert window.table.rowCount() == 2
    assert window.status.text() == "2 files, 5 lines"
    assert window.header.text() == "notes"  # no longer busy
