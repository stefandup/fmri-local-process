"""Debug harness for step 0: drive the real PlainWindow (toy_gui_0_plain.py) one action at a
time, so you can step through it in VS Code.

Not a test and not part of the app: it calls the toy's real classes and methods with fixed,
visible inputs. Nothing in toy_gui_0_plain.py is changed for it.

Run it from Run and Debug -> "Debug toy step 0". Without the debugger it just prints.

The script has two parts:
  Part A  counts lines by calling CountThread.run() directly, on this (main) thread
  Part B  counts lines the way the app does: start_counting() starts a real worker thread
"""

import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from toy_data import make_folder
from toy_gui_0_plain import CountThread, PlainWindow

# ---- Inputs: every value the run depends on -------------------------------------------------

# Where this harness keeps its files: <your temp folder>\toy_debug\step0. Not the repo, not the
# registry, so it never touches the real app's saved settings.
WORK_FOLDER: Path = Path(tempfile.gettempdir()) / "toy_debug" / "step0"

# The settings file the window reads and writes (the real app uses the registry instead).
SETTINGS_FILE: Path = WORK_FOLDER / "settings.ini"

# Seconds the worker waits after each file. 0 = no waiting; the app's main() uses 1.0.
PAUSE_SECONDS: float = 0.0

# True: show the window and repaint it after each step, so you can see each change on screen.
SHOW_WINDOW: bool = True


def refresh_screen(app: QApplication) -> None:
    """Let Qt handle what is waiting: repaint the window, deliver queued signals.

    The real app has an event loop (app.exec()) that does this all the time. This harness
    has none, so that it can stop between steps. It calls this after each step instead."""
    app.processEvents()


def main() -> None:
    # ---- 1. Qt setup ------------------------------------------------------------------------
    # Exactly one QApplication per program, created before any widget.
    app = QApplication.instance() or QApplication(sys.argv)
    assert isinstance(app, QApplication)

    # ---- 2. Input files ---------------------------------------------------------------------
    # Start from no saved settings, so the window behaves like a first launch every time.
    SETTINGS_FILE.unlink(missing_ok=True)

    # The folder to count: WORK_FOLDER/notes with a.txt, b.txt and skip.csv (see toy_data.py).
    notes_folder: Path = make_folder(WORK_FOLDER)
    files_in_folder: list[str] = sorted(path.name for path in notes_folder.iterdir())

    # The settings store the window is handed: QSettings on a plain-text .ini file.
    settings = QSettings(str(SETTINGS_FILE), QSettings.Format.IniFormat)

    # ---- 3. Build the window ----------------------------------------------------------------
    window = PlainWindow(settings, pause=PAUSE_SECONDS)
    folder_after_build: Path | None = window.folder  # what __init__ read from settings
    header_after_build: str = window.header.text()
    if SHOW_WINDOW:
        window.resize(500, 350)
        window.show()
        refresh_screen(app)

    # ---- 4. Choose the folder, as a user would on tab 1 -------------------------------------
    folder_text: str = str(notes_folder)  # what the user would type into the box
    window.path_edit.setText(folder_text)
    window.use_button.click()  # Qt calls the connected slot, window.use_folder()
    refresh_screen(app)

    folder_after_click: Path | None = window.folder
    header_after_click: str = window.header.text()
    settings.sync()  # write the settings file now (QSettings normally does it a bit later)
    saved_folder_value: object = settings.value("folder")
    settings_file_text: str = SETTINGS_FILE.read_text()

    # ---- 5. Part A: the counting loop on this thread ----------------------------------------
    # run() is the code the worker thread would run. Called directly, it runs right here.
    window.tabs.setCurrentIndex(1)  # show tab 2, "Line counts"
    stepping_thread = CountThread(notes_folder, pause=PAUSE_SECONDS)
    stepping_thread.file_counted.connect(window.add_row)
    stepping_thread.all_done.connect(window.finish)
    stepping_thread.run()
    refresh_screen(app)

    rows_after_part_a: int = window.table.rowCount()
    status_after_part_a: str = window.status.text()

    # ---- 6. Part B: the same counting, the way the app does it ------------------------------
    window.start_counting()  # makes a new CountThread and calls .start(): a real worker thread
    busy_right_after_start: bool = window.busy
    header_right_after_start: str = window.header.text()

    worker_thread = window.counter
    assert worker_thread is not None
    worker_thread.wait()  # block here until the worker's run() has returned
    rows_before_delivery: int = window.table.rowCount()

    refresh_screen(app)  # deliver the signals the worker queued, to add_row and finish
    rows_after_part_b: int = window.table.rowCount()
    status_after_part_b: str = window.status.text()
    busy_at_end: bool = window.busy
    header_at_end: str = window.header.text()

    # ---- 7. What happened -------------------------------------------------------------------
    print(f"files in folder:          {files_in_folder}")
    print(f"after build:              folder={folder_after_build}  header={header_after_build!r}")
    print(f"after click:              folder={folder_after_click}  header={header_after_click!r}")
    print(f"saved in settings:        {saved_folder_value!r}")
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
