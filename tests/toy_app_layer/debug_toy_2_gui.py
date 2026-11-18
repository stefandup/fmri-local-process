"""Debug harness for step 2: the Qt window after the split (toy_gui.py), driving the app
layer. The same actions as debug_toy_0_plain_gui.py, so you can compare the two runs.

Not a test and not part of the app: it calls the toy's real classes with fixed, visible
inputs. Run it from Run and Debug -> "Debug toy step 2". Without the debugger it just prints.

The script has two parts, as in step 0:
  Part A  counts by calling CountThread.run() directly, on this (main) thread
  Part B  counts the way the app does: page.start() starts a real worker thread
"""

import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from toy_app import COUNT_TAB, FOLDER_TAB, ToolboxState
from toy_data import make_folder
from toy_gui import CountPage, CountThread, FolderPage, ToolboxWindow

# ---- Inputs ---------------------------------------------------------------------------------

# Where this harness keeps its files: <your temp folder>\toy_debug\step2 (not the repo).
WORK_FOLDER: Path = Path(tempfile.gettempdir()) / "toy_debug" / "step2"

# The settings file the state reads and writes (the real app uses the registry instead).
SETTINGS_FILE: Path = WORK_FOLDER / "settings.ini"

# True: show the window and repaint it after each step, so you can see each change on screen.
SHOW_WINDOW: bool = True


def refresh_screen(app: QApplication) -> None:
    """Let Qt handle what is waiting: repaint, deliver queued signals. The real app's event
    loop (app.exec()) does this all the time; this harness has none, so it can stop between
    steps, and calls this after each step instead."""
    app.processEvents()


def main() -> None:
    # ---- 1. Qt setup ------------------------------------------------------------------------
    app = QApplication.instance() or QApplication(sys.argv)
    assert isinstance(app, QApplication)

    # ---- 2. Input files ---------------------------------------------------------------------
    SETTINGS_FILE.unlink(missing_ok=True)  # start like a first launch
    notes_folder: Path = make_folder(WORK_FOLDER)  # a.txt, b.txt, skip.csv (see toy_data.py)
    settings = QSettings(str(SETTINGS_FILE), QSettings.Format.IniFormat)

    # ---- 3. Build the state and the window --------------------------------------------------
    # New in step 2: the state is its own object, made first and handed to the window.
    state = ToolboxState(settings)
    window = ToolboxWindow(state)  # Step Into: pages are built from TAB_ORDER
    header_after_build: str = window.header.text()

    folder_page = window.pages[FOLDER_TAB]
    count_page = window.pages[COUNT_TAB]
    assert isinstance(folder_page, FolderPage)
    assert isinstance(count_page, CountPage)

    if SHOW_WINDOW:
        window.resize(500, 350)
        window.show()
        refresh_screen(app)

    # ---- 4. Choose the folder on tab 1 ------------------------------------------------------
    folder_text: str = str(notes_folder)
    folder_page.path_edit.setText(folder_text)
    folder_page.use_button.click()  # Qt calls the lambda in FolderPage -> state.set_folder()
    refresh_screen(app)

    folder_in_state: Path | None = state.folder
    header_after_click: str = window.header.text()
    settings.sync()  # write the settings file now (QSettings normally does it a bit later)
    settings_file_text: str = SETTINGS_FILE.read_text()

    # ---- 5. Part A: the counting on this thread ---------------------------------------------
    window.tabs.setCurrentIndex(1)  # show tab 2, "Line counts"
    stepping_thread = CountThread(notes_folder)  # makes a CountJob with itself as listener
    stepping_thread.file_counted.connect(count_page.add_row)
    stepping_thread.all_done.connect(count_page.finish)
    stepping_thread.run()  # Step Into: run() -> job.run() -> on_file() -> emit
    refresh_screen(app)

    rows_after_part_a: int = count_page.table.rowCount()
    status_after_part_a: str = count_page.status.text()

    # ---- 6. Part B: the same counting, the way the app does it ------------------------------
    count_page.start()  # makes a new CountThread and calls .start(): a real worker thread
    busy_right_after_start: set[str] = set(state.busy)  # a copy, so later changes don't show
    header_right_after_start: str = window.header.text()

    worker_thread = count_page.counter
    assert worker_thread is not None
    worker_thread.wait()  # block here until the worker's run() has returned
    rows_before_delivery: int = count_page.table.rowCount()

    refresh_screen(app)  # deliver the signals the worker queued, to add_row and finish
    rows_after_part_b: int = count_page.table.rowCount()
    status_after_part_b: str = count_page.status.text()
    busy_at_end: set[str] = set(state.busy)
    header_at_end: str = window.header.text()

    # ---- 7. What happened -------------------------------------------------------------------
    print(f"after build:              header={header_after_build!r}")
    print(
        f"after click:              state.folder={folder_in_state}  header={header_after_click!r}"
    )
    print(f"settings file:\n{settings_file_text}")
    print(f"part A:                   rows={rows_after_part_a}  status={status_after_part_a!r}")
    print(
        f"part B right after start: busy={busy_right_after_start}  "
        f"header={header_right_after_start!r}"
    )
    print(f"part B after wait():      rows={rows_before_delivery}")
    print(
        f"part B after delivery:    rows={rows_after_part_b}  status={status_after_part_b!r}  "
        f"busy={busy_at_end}  header={header_at_end!r}"
    )

    window.close()


if __name__ == "__main__":
    main()
