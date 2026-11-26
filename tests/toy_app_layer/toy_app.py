"""Layer 2, app/: what the window and the terminal app share, written once. No Qt in here.

Four pieces, each the toy version of something that now sits inside gui/:
1. Event        a Qt-signal look-alike, so GUI code keeps writing .connect() / .emit()
2. Settings     where remembered values go: the GUI plugs in QSettings, the TUI a JSON file
3. ToolboxState the shared folder + busy locks          (real: gui/toolbox_state.py)
4. CountJob     a background job's loop + stop flag     (real: the loop in PreprocessThread.run)
Plus the tab list and the texts both UIs show           (real: TAB_ORDER, report_lines, ...)
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from toy_processing import count_lines, text_files

# ---- 1. Event -------------------------------------------------------------------------------


class Event:
    """A list of callbacks: `ev.connect(f)`, then `ev.emit(x)` calls `f(x)`.

    Spelled like a Qt Signal, so moving code out of Qt barely changes the call sites. Only
    emit on the UI thread; a job on a worker thread reports through its listener instead."""

    def __init__(self) -> None:
        self._callbacks: list[Callable[..., object]] = []

    def connect(self, callback: Callable[..., object]) -> None:
        self._callbacks.append(callback)

    def emit(self, *args: object) -> None:
        for callback in self._callbacks:
            callback(*args)


# ---- 2. Settings ----------------------------------------------------------------------------


class Settings(Protocol):
    """What the state needs from a settings store. A Protocol only describes methods, so any
    object with them fits, including Qt's own QSettings, unchanged. (The `/` makes the
    arguments positional, so QSettings' different argument names don't matter.)"""

    def value(self, key: str, default: Any = None, /) -> Any: ...
    def setValue(self, key: str, value: Any, /) -> None: ...  # noqa: N802 -- Qt's spelling


class JsonSettings:
    """The TUI's settings store: one JSON file (a headless Linux server has no registry)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = json.loads(path.read_text()) if path.is_file() else {}

    def value(self, key: str, default: Any = None, /) -> Any:
        return self.data.get(key, default)

    def setValue(self, key: str, value: Any, /) -> None:  # noqa: N802
        self.data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))


# ---- 3. Shared state ------------------------------------------------------------------------

COUNTING = "counting"  # a job name, like CONVERTING / PREPROCESSING in the toolbox


class ToolboxState:
    """The folder every tab works on and what is running. Both UIs `.connect()` to the events
    to refresh what they show. The settings store is passed in: the state doesn't care which."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.folder_changed = Event()
        self.busy_changed = Event()
        saved = str(settings.value("folder", "") or "")
        self.folder: Path | None = Path(saved) if saved else None
        self.busy: set[str] = set()

    def set_folder(self, folder: Path) -> None:
        if folder == self.folder:
            return
        self.folder = folder
        self.settings.setValue("folder", str(folder))  # remembered for next time
        self.folder_changed.emit()

    def set_busy(self, job: str, running: bool) -> None:
        if running:
            self.busy.add(job)
        else:
            self.busy.discard(job)
        self.busy_changed.emit()

    def lock_reason(self) -> str:
        """Why changing actions are locked now; '' when they aren't."""
        return ", ".join(sorted(self.busy))


# ---- 4. A background job --------------------------------------------------------------------


@dataclass
class CountSummary:
    counts: dict[str, int] = field(default_factory=dict)
    stopped: bool = False


class CountListener(Protocol):
    """What a job tells the UI while it runs. Each UI supplies an object with these methods."""

    def on_file(self, name: str, lines: int, done: int, total: int) -> None: ...
    def on_done(self, summary: CountSummary) -> None: ...


class CountJob:
    """The loop that today lives inside a QThread's run(). It runs on any thread and knows
    nothing about Qt or Textual: it only calls its listener. Each UI turns those calls into
    its own safe way of updating the screen (Qt: a signal; Textual: call_from_thread)."""

    def __init__(self, folder: Path, listener: CountListener) -> None:
        self.folder = folder
        self.listener = listener
        self.stop_requested = False

    def stop(self) -> None:
        """Safe from any thread: just a flag the loop checks before each file."""
        self.stop_requested = True

    def run(self) -> CountSummary:
        summary = CountSummary()
        files = text_files(self.folder)
        for done, path in enumerate(files, start=1):
            if self.stop_requested:
                summary.stopped = True
                break
            summary.counts[path.name] = lines = count_lines(path)
            self.listener.on_file(path.name, lines, done, len(files))
        self.listener.on_done(summary)
        return summary


# ---- Tab list and texts both UIs show -------------------------------------------------------

FOLDER_TAB = "Folder"
COUNT_TAB = "Line counts"
TAB_ORDER = (FOLDER_TAB, COUNT_TAB)  # one list: both UIs build their tabs from it


def tab_text(number: int, name: str) -> str:
    return f"{number}. {name}"


def header_text(state: ToolboxState) -> str:
    folder = state.folder.name if state.folder else "No folder chosen"
    busy = state.lock_reason()
    return f"{folder}  ⏳ {busy}" if busy else folder


def summary_text(summary: CountSummary) -> str:
    total = sum(summary.counts.values())
    text = f"{len(summary.counts)} files, {total} lines"
    return text + " (stopped)" if summary.stopped else text
