"""Layer 3b, tui/: the same tool in a terminal, with Textual. Same app-layer objects and
tab list, different widgets. Never imports PySide6 (test_toy_4_guards checks that).

    Qt (toy_gui.py)            Textual (here)
    QMainWindow + QTabWidget   App + TabbedContent / TabPane
    QLabel                     Static
    QLineEdit / QPushButton    Input / Button   (clicks arrive in on_button_pressed)
    QTableWidget               DataTable
    QThread + signals          run_worker(thread=True) + call_from_thread

Run it: `python tests/toy_app_layer/toy_tui.py` (`q` quits).
"""

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, DataTable, Footer, Input, Static, TabbedContent, TabPane
from toy_app import (
    COUNT_TAB,
    COUNTING,
    FOLDER_TAB,
    TAB_ORDER,
    CountJob,
    CountSummary,
    JsonSettings,
    ToolboxState,
    header_text,
    summary_text,
    tab_text,
)


class FolderPane(Vertical):
    """Tab 1, like toy_gui.FolderPage."""

    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state

    def compose(self) -> ComposeResult:  # Textual's way of adding child widgets
        yield Input(str(self.state.folder or ""), placeholder="Folder", id="folder-input")
        yield Button("Use this folder", id="use-folder")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "use-folder":
            self.state.set_folder(Path(self.query_one("#folder-input", Input).value))


class TuiListener:
    """The job's listener in the TUI. Calls arrive on the worker thread; call_from_thread
    runs each one on the UI thread, the Textual counterpart of emitting a Qt signal."""

    def __init__(self, pane: "CountPane") -> None:
        self.pane = pane

    def on_file(self, name: str, lines: int, done: int, total: int) -> None:
        self.pane.app.call_from_thread(self.pane.add_row, name, lines, done, total)

    def on_done(self, summary: CountSummary) -> None:
        self.pane.app.call_from_thread(self.pane.finish, summary)


class CountPane(Vertical):
    """Tab 2, like toy_gui.CountPage: same checks, same texts, from the app layer."""

    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        self.job: CountJob | None = None

    def compose(self) -> ComposeResult:
        yield Button("Count lines", id="run")
        yield DataTable(id="counts")
        yield Static(id="status")

    def on_mount(self) -> None:  # runs once the widgets exist
        self.query_one(DataTable).add_columns("File", "Lines")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run":
            self.start()

    def start(self) -> None:
        if self.state.folder is None or self.state.lock_reason():
            self.set_status(f"Can't start: {self.state.lock_reason() or 'no folder'}")
            return
        self.query_one(DataTable).clear()
        self.job = CountJob(self.state.folder, TuiListener(self))
        self.state.set_busy(COUNTING, True)
        self.run_worker(self.job.run, thread=True)  # job.run() on a worker thread

    def add_row(self, name: str, lines: int, done: int, total: int) -> None:
        self.query_one(DataTable).add_row(name, lines)
        self.set_status(f"{done} of {total}")

    def finish(self, summary: CountSummary) -> None:
        self.state.set_busy(COUNTING, False)
        self.set_status(summary_text(summary))

    def set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)


PAGES = {FOLDER_TAB: FolderPane, COUNT_TAB: CountPane}  # tab name -> pane class


class ToyTui(App):
    """The header and the tabs, built from the shared TAB_ORDER, like toy_gui.ToolboxWindow."""

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with TabbedContent():
            for number, name in enumerate(TAB_ORDER, start=1):
                with TabPane(tab_text(number, name), id=f"tab-{number}"):
                    yield PAGES[name](self.state)
        yield Footer()

    def on_mount(self) -> None:
        # The same app-layer events the Qt window connects to.
        self.state.folder_changed.connect(self.update_header)
        self.state.busy_changed.connect(self.update_header)
        self.update_header()

    def update_header(self) -> None:
        self.query_one("#header", Static).update(header_text(self.state))


if __name__ == "__main__":
    settings = JsonSettings(Path.home() / ".config" / "toy_toolbox" / "settings.json")
    ToyTui(ToolboxState(settings)).run()
