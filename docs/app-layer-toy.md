# The app/ layer, shown on a toy

A small working example of phases A and B from [Next steps](next-steps.md): pulling the
logic out of the Qt window into a Qt-free `app/` layer, then building a terminal app (TUI)
on that same layer. The code is in `tests/toy_app_layer/`. It is temporary and goes once
phase A is done.

The toy counts the lines in the `.txt` files of a folder. It is tiny, but it has the parts
of the toolbox that matter here: a shared folder, a "busy" lock, a background job with
progress and a stop flag, and numbered tabs.

## The layers

```
toy_gui_0_plain.py  0. all in one Qt window (before)      real: gui/ as it is today
toy_processing.py   1. the work: plain functions          real: processing/ (unchanged)
toy_app.py          2. shared, no Qt                      real: the new app/
toy_gui.py          3a. PySide6 window, display only      real: gui/
toy_tui.py          3b. Textual terminal app, display only real: the new tui/
```

The two front ends never talk to each other: both use `toy_app.py`, and only that.

## 1. Events without Qt

Qt pages react to signals (`state.folder_changed.connect(...)`). The app layer can't use Qt
signals, so it has a tiny look-alike with the same spelling, which leaves the GUI's call
sites unchanged:

```python
class Event:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in self._callbacks:
            callback(*args)
```

Events are only emitted on the UI thread. A job on a worker thread reports in a different
way (see 4).

## 2. Settings that either front end can supply

The state needs somewhere to remember the folder. A `Protocol` lists the methods it needs.
Qt's `QSettings` already has them, so the window passes one in unchanged (the registry on
Windows, so existing users keep their folders). The TUI passes a `JsonSettings` (one JSON
file, because a Linux server has no registry).

```python
class Settings(Protocol):
    def value(self, key: str, default: Any = None, /) -> Any: ...
    def setValue(self, key: str, value: Any, /) -> None: ...

ToolboxState(QSettings(...))                      # the window
ToolboxState(JsonSettings(Path("settings.json"))) # the TUI
```

## 3. Shared state

`ToolboxState` holds the folder and the running jobs and announces changes. Both front
ends connect their header to it:

```python
state.folder_changed.connect(self.update_header)
state.busy_changed.connect(self.update_header)
```

The header text itself comes from the app layer (`header_text(state)`), so both front ends
show the same thing.

## 4. A background job with a listener

This is the key step. Today the loop sits inside a `QThread`, so only Qt can run it:

```python
class CountThread(QThread):                      # BEFORE
    def run(self):
        for path in text_files(self.folder):     # the logic lives here
            self.file_counted.emit(path.name, count_lines(path))
```

After the move, the loop is a plain class in the app layer. It reports to a **listener**
(any object with `on_file` and `on_done`) and checks a stop flag between files:

```python
class CountJob:                                  # AFTER, in app/
    def run(self) -> CountSummary:
        for done, path in enumerate(files, start=1):
            if self.stop_requested:
                ...
            self.listener.on_file(path.name, lines, done, len(files))
        self.listener.on_done(summary)
```

Each front end supplies its own listener. Each one turns the calls from the worker thread
into safe screen updates:

| | Window (Qt) | Terminal (Textual) |
|---|---|---|
| Runs the job on a thread | `QThread.run()` calls `job.run()` | `self.run_worker(job.run, thread=True)` |
| Listener passes progress to the UI thread | emits a signal (`self.file_counted.emit(...)`) | `app.call_from_thread(pane.add_row, ...)` |
| Tests drive it with | `thread.wait()` + `QApplication.processEvents()` | `await app.workers.wait_for_complete()` + `pilot.pause()` |

In tests, a `FakeListener` that just records the calls is enough: the job runs without any
UI or thread at all. Most of the real logic gets tested this way after phase A.

## 5. One tab list for both

```python
TAB_ORDER = (FOLDER_TAB, COUNT_TAB)
PAGES = {FOLDER_TAB: FolderPage, COUNT_TAB: CountPage}     # toy_gui.py
PAGES = {FOLDER_TAB: FolderPane, COUNT_TAB: CountPane}     # toy_tui.py
```

Both front ends build their numbered tabs from `TAB_ORDER`.

## Qt and Textual side by side

| Qt (`toy_gui.py`) | Textual (`toy_tui.py`) |
|---|---|
| `QMainWindow` + `QTabWidget` | `App` + `TabbedContent` / `TabPane` |
| `QLabel` | `Static` |
| `QLineEdit` / `QPushButton` | `Input` / `Button` (clicks arrive in `on_button_pressed`) |
| `QTableWidget` | `DataTable` |
| widgets added in `__init__` | widgets yielded from `compose()` |
| `QThread` + signals | `run_worker(thread=True)` + `call_from_thread` |

## Two guards that keep the front ends in step

`test_toy_4_guards.py`:

- **Tab parity:** every tab in `TAB_ORDER` must be in both `PAGES` dicts. A tab added
  only to the window fails the tests. The real version will allow an explicit
  "not yet in the TUI" list.
- **No Qt in the TUI:** a fresh Python imports the TUI and checks that PySide6 wasn't
  loaded. On a headless server, Qt may not even be installed.

## Stepping through it

First run the plain window, then the split one. Both pause 1 s per file so you can see
the thread at work:

    python tests/toy_app_layer/toy_gui_0_plain.py
    python tests/toy_app_layer/toy_gui.py

Then run the tests in this order (VS Code Test Explorer, or `pytest tests/toy_app_layer -v`):

0. `test_toy_0_plain_gui.py`: the plain window, and how to read a Qt test
1. `test_toy_1_app.py`: the app layer alone, with a fake listener
2. `test_toy_2_gui.py`: the window, offscreen
3. `test_toy_3_tui.py`: the terminal app, through Textual's test pilot (the same two
   scenarios as step 2)
4. `test_toy_4_guards.py`: the two guards

Try the terminal app yourself: `python tests/toy_app_layer/toy_tui.py` (`q` quits).
