"""Debug harness for step 1: the app layer (toy_app.py) on its own. No window, no terminal,
no threads, so Step Into (F11) follows every call.

Not a test and not part of the app: it calls the toy's real classes with fixed, visible
inputs. Run it from Run and Debug -> "Debug toy step 1". Without the debugger it just prints.

Five parts, one per piece of toy_app.py:
  1  Event          connect a function, emit, see it called
  2  ToolboxState   choose a folder, see it saved to the JSON settings file and read back
  3  busy lock      mark a job running, see the header text change
  4  CountJob       count the notes folder, see each file reported to a listener
  5  stopping       stop the job after the first file
"""

import tempfile
from pathlib import Path

from toy_app import (
    COUNTING,
    CountJob,
    CountSummary,
    Event,
    JsonSettings,
    ToolboxState,
    header_text,
    summary_text,
)
from toy_data import make_folder

# ---- Inputs ---------------------------------------------------------------------------------

# Where this harness keeps its files: <your temp folder>\toy_debug\step1 (not the repo).
WORK_FOLDER: Path = Path(tempfile.gettempdir()) / "toy_debug" / "step1"

# The JSON settings file ToolboxState is handed (the TUI's kind of settings store).
SETTINGS_FILE: Path = WORK_FOLDER / "settings.json"


# ---- Listeners: the job's audience ----------------------------------------------------------
# CountJob reports to "a listener": any object with on_file() and on_done(). In the app the
# listener is the UI. These two are the smallest real ones: they write down what they hear.


class RecordingListener:
    """Writes down every call from the job, in order."""

    def __init__(self) -> None:
        self.files_heard: list[tuple[str, int, int, int]] = []  # (name, lines, done, total)
        self.summary_heard: CountSummary | None = None

    def on_file(self, name: str, lines: int, done: int, total: int) -> None:
        call = (name, lines, done, total)
        self.files_heard.append(call)

    def on_done(self, summary: CountSummary) -> None:
        self.summary_heard = summary


class StopAfterFirstFileListener(RecordingListener):
    """Like RecordingListener, but presses "stop" as soon as the first file is in, as a
    stop button would. It needs the job to stop, so the job is handed to it after both exist."""

    def __init__(self) -> None:
        super().__init__()
        self.job: CountJob | None = None

    def on_file(self, name: str, lines: int, done: int, total: int) -> None:
        super().on_file(name, lines, done, total)
        if self.job is not None:
            self.job.stop()


def main() -> None:
    SETTINGS_FILE.unlink(missing_ok=True)  # start like a first launch
    notes_folder: Path = make_folder(WORK_FOLDER)  # a.txt, b.txt, skip.csv (see toy_data.py)

    # ---- 1. Event ---------------------------------------------------------------------------
    messages_heard: list[str] = []

    def remember_message(message: str) -> None:
        messages_heard.append(message)

    greeting_event = Event()
    greeting_event.connect(remember_message)
    greeting_event.emit("hello")  # Step Into: emit() loops over its callbacks

    # ---- 2. ToolboxState and settings -------------------------------------------------------
    settings = JsonSettings(SETTINGS_FILE)
    state = ToolboxState(settings)
    folder_at_start: Path | None = state.folder

    folders_announced: list[Path | None] = []

    def on_folder_changed() -> None:  # what a UI would do: read the state, refresh the screen
        folders_announced.append(state.folder)

    state.folder_changed.connect(on_folder_changed)

    state.set_folder(notes_folder)  # Step Into: saves, then emits folder_changed
    state.set_folder(notes_folder)  # the same folder again: watch which line returns
    settings_file_text: str = SETTINGS_FILE.read_text()

    # A brand-new state on the same file = the next time the app starts.
    next_launch_state = ToolboxState(JsonSettings(SETTINGS_FILE))
    folder_at_next_launch: Path | None = next_launch_state.folder

    # ---- 3. Busy lock -----------------------------------------------------------------------
    header_when_idle: str = header_text(state)
    state.set_busy(COUNTING, True)
    lock_reason_while_busy: str = state.lock_reason()
    header_while_busy: str = header_text(state)
    state.set_busy(COUNTING, False)
    header_after_busy: str = header_text(state)

    # ---- 4. CountJob ------------------------------------------------------------------------
    listener = RecordingListener()
    job = CountJob(notes_folder, listener)
    summary: CountSummary = job.run()  # runs right here; no thread in this step
    text_for_summary: str = summary_text(summary)

    # ---- 5. Stopping ------------------------------------------------------------------------
    stopping_listener = StopAfterFirstFileListener()
    stopping_job = CountJob(notes_folder, stopping_listener)
    stopping_listener.job = stopping_job
    stopped_summary: CountSummary = stopping_job.run()
    text_for_stopped_summary: str = summary_text(stopped_summary)

    # ---- What happened ----------------------------------------------------------------------
    print(f"1 messages heard:        {messages_heard}")
    print(f"2 folder at start:       {folder_at_start}")
    print(f"  folders announced:     {folders_announced}")
    print(f"  settings file:         {settings_file_text}")
    print(f"  folder at next launch: {folder_at_next_launch}")
    print(
        f"3 headers:               {header_when_idle!r} -> {header_while_busy!r} "
        f"-> {header_after_busy!r}  (lock reason {lock_reason_while_busy!r})"
    )
    print(f"4 files heard:           {listener.files_heard}")
    print(f"  summary:               {summary}  -> {text_for_summary!r}")
    print(f"5 files heard:           {stopping_listener.files_heard}")
    print(f"  summary:               {stopped_summary}  -> {text_for_stopped_summary!r}")


if __name__ == "__main__":
    main()
