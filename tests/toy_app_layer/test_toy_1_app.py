"""Step 1: the app layer on its own. No window, no terminal: plain objects and a fake
listener. This is where most of the real logic gets tested after phase A."""

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


class FakeListener:
    """Stands in for a UI: just records what the job tells it."""

    def __init__(self) -> None:
        self.files: list[tuple[str, int]] = []
        self.summary: CountSummary | None = None

    def on_file(self, name: str, lines: int, done: int, total: int) -> None:
        self.files.append((name, lines))

    def on_done(self, summary: CountSummary) -> None:
        self.summary = summary


def test_event_calls_what_was_connected() -> None:
    heard = []
    event = Event()
    event.connect(heard.append)
    event.emit("hello")
    assert heard == ["hello"]


def test_state_remembers_the_folder(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    state = ToolboxState(JsonSettings(settings_file))
    changes = []
    state.folder_changed.connect(lambda: changes.append(state.folder))

    state.set_folder(tmp_path / "notes")
    state.set_folder(tmp_path / "notes")  # same folder again: no second event

    assert changes == [tmp_path / "notes"]
    # A new state (= the next launch) reads it back from the settings file.
    assert ToolboxState(JsonSettings(settings_file)).folder == tmp_path / "notes"


def test_busy_lock_shows_in_the_header(tmp_path: Path) -> None:
    state = ToolboxState(JsonSettings(tmp_path / "s.json"))
    state.set_folder(tmp_path / "notes")
    state.set_busy(COUNTING, True)
    assert state.lock_reason() == COUNTING
    assert header_text(state) == "notes  ⏳ counting"
    state.set_busy(COUNTING, False)
    assert header_text(state) == "notes"


def test_job_reports_each_file(tmp_path: Path) -> None:
    listener = FakeListener()
    summary = CountJob(make_folder(tmp_path), listener).run()  # runs right here, no thread
    assert listener.files == [("a.txt", 2), ("b.txt", 3)]
    assert listener.summary is summary
    assert summary_text(summary) == "2 files, 5 lines"


def test_job_stops_between_files(tmp_path: Path) -> None:
    listener = FakeListener()
    job = CountJob(make_folder(tmp_path), listener)
    # Press "stop" as soon as the first file is in (a UI would call job.stop() from a button).
    listener.on_file = lambda *args: job.stop()  # type: ignore[method-assign]
    summary = job.run()
    assert list(summary.counts) == ["a.txt"]
    assert summary_text(summary) == "1 files, 2 lines (stopped)"
