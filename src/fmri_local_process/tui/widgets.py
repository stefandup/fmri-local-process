"""Pieces the terminal app's tabs are built from."""

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Input,
    OptionList,
    ProgressBar,
    RichLog,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from fmri_local_process.app.colors import ERROR_COLOR, WARNING_COLOR
from fmri_local_process.app.docker import docker_line
from fmri_local_process.app.image_download import ImageState, PullJob, remove
from fmri_local_process.app.plain import plain
from fmri_local_process.app.run_progress import SPINNER, BatchProgress
from fmri_local_process.app.settings import Settings
from fmri_local_process.app.state import ToolboxState, folder_conflict, folder_locked

if TYPE_CHECKING:
    from fmri_local_process.tui.toolbox import ToolboxApp

log = logging.getLogger(__name__)

TICKED, UNTICKED = "☑", "☐"
CREATED_IF_MISSING = ("bids", "output", "mriqc")  # folders the toolbox writes
SPINNER_S = 0.5


def cell(text: object, color: str = "") -> Text:
    """Text as it is (a '[' in a name isn't markup), in a colour."""
    return Text(str(text), style=color)


def on_background(text: object, color: str) -> Text:
    return Text(str(text), style=f"black on {color}")


def whole_number(text: str, low: int, high: int) -> int | None:
    """The number typed, None when it isn't a whole number from low to high."""
    try:
        value = int(text)
    except ValueError:
        return None
    return value if low <= value <= high else None


def decimal(text: str, low: float, high: float) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if low <= value <= high else None


def option_text(value: object) -> str | None:
    """A Select's chosen value, None when nothing is chosen."""
    return value if isinstance(value, str) else None


# ---- screens over the app ------------------------------------------------------------------------


class ConfirmScreen(ModalScreen[str]):
    """A question with buttons; gives back the button's text ('' for Escape)."""

    BINDINGS = [
        Binding("escape", "dismiss('')", "Cancel"),
        Binding("y", "answer('Yes')", "Yes"),
        Binding("n", "answer('No')", "No"),
    ]

    def __init__(self, title: str, text: str, buttons: Sequence[str] = ("Yes", "No")) -> None:
        super().__init__()
        self.title_text, self.text, self.buttons = title, text, buttons

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            with VerticalScroll(classes="dialog-body"):
                yield Static(self.text, markup=False)
            with Horizontal(classes="buttons"):
                for text in self.buttons:
                    yield Button(text)

    def action_answer(self, text: str) -> None:
        if text in self.buttons:
            self.dismiss(text)

    def on_mount(self) -> None:
        self.query(Button).last().focus()  # the safe answer (No / Cancel) is under Enter

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(str(event.button.label))


class TextScreen(ModalScreen[None]):
    """A text to read: help, a message, formulas."""

    BINDINGS = [Binding("escape,q", "dismiss", "Close")]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.title_text, self.text = title, text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            with VerticalScroll(classes="dialog-body"):
                yield Static(self.text, markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Close")

    def on_button_pressed(self) -> None:
        self.dismiss()


class PickScreen(ModalScreen[int | None]):
    """One of several; gives back its index."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, choices: Sequence[str]) -> None:
        super().__init__()
        self.title_text, self.choices = title, choices

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            yield OptionList(*(Option(Text(choice)) for choice in self.choices))
            with Horizontal(classes="buttons"):
                yield Button("Cancel")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index)

    def on_button_pressed(self) -> None:
        self.dismiss(None)


class InputScreen(ModalScreen[str | None]):
    """A line of text; None when cancelled."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, value: str = "") -> None:
        super().__init__()
        self.title_text, self.value = title, value

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            yield Input(self.value)
            with Horizontal(classes="buttons"):
                yield Button("OK", id="ok", variant="primary")
                yield Button("Cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one(Input).value if event.button.id == "ok" else None)


class FoldersOnly(DirectoryTree):
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [p for p in paths if p.is_dir() and not p.name.startswith(".")]


class BrowseScreen(ModalScreen[Path | None]):
    """Walk the folders: Enter opens one, 'Choose' takes the highlighted one."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel"), ("u", "up", "Folder above")]

    def __init__(self, title: str, start: Path) -> None:
        super().__init__()
        self.title_text, self.start = title, start

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            yield Static(str(self.start), id="root", markup=False)
            yield FoldersOnly(self.start)
            with Horizontal(classes="buttons"):
                yield Button("Choose", id="choose", variant="primary")
                yield Button("Folder above (u)", id="up")
                yield Button("Cancel", id="cancel")

    def action_up(self) -> None:
        tree = self.query_one(FoldersOnly)
        tree.path = Path(tree.path).parent
        self.query_one("#root", Static).update(str(tree.path))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "up":
            self.action_up()
            return
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        tree = self.query_one(FoldersOnly)
        node = tree.cursor_node
        self.dismiss(node.data.path if node is not None and node.data else Path(tree.path))


# ---- the tabs ------------------------------------------------------------------------------------


class Page(VerticalScroll):
    """One tab. show_page() is called whenever it is shown or its data changed."""

    GUIDE = ""  # shown with ?

    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state

    @property
    def toolbox(self) -> "ToolboxApp":
        return cast("ToolboxApp", self.app)

    def show_page(self) -> None: ...

    def help_text(self) -> str:
        return plain(self.GUIDE)

    # ---- background work ----------------------------------------------------------------------

    def start_job(
        self,
        work: Callable[[], Any],
        done: Callable[[Any], object],
        failed: Callable[[str], object] | None = None,
    ) -> None:
        """work() on a thread, then done(result) on the UI thread. A daemon thread: quitting
        doesn't wait for hours of C-PAC."""

        def body() -> None:
            try:
                result = work()
            except Exception as e:  # noqa: BLE001 -- a failed job mustn't take the app down
                log.exception("%s failed", type(self).__name__)
                self.tell(failed or self.job_failed, f"{type(e).__name__}: {e}")
            else:
                self.tell(done, result)
            finally:
                self.toolbox.jobs.discard(thread)

        thread = threading.Thread(target=body, daemon=True)
        self.toolbox.jobs.add(thread)
        thread.start()

    def job_failed(self, error: str) -> None:
        self.show_text("Stopped unexpectedly", f"{error}\n\nPlease pass this on.")

    def tell(self, callback: Callable[..., object], *args: object) -> None:
        """From a job's thread: callback(*args) on the UI thread (dropped once the app ends)."""
        try:
            self.app.call_from_thread(callback, *args)
        except RuntimeError:  # the app has closed meanwhile
            log.debug("app closed: %s not shown", callback)

    # ---- asking and showing -------------------------------------------------------------------

    def ask(
        self,
        title: str,
        text: str,
        yes: Callable[[], object],
    ) -> None:
        """yes() when the question is answered Yes."""

        def answered(answer: str | None) -> None:
            if answer == "Yes":
                yes()

        self.app.push_screen(ConfirmScreen(title, text), answered)

    def show_text(self, title: str, text: str) -> None:
        self.app.push_screen(TextScreen(title, text))

    def ask_save_path(self, title: str, start: Path, save: Callable[[Path], object]) -> None:
        """save(path) once a path is given; asks before replacing a file."""

        def given(text: str | None) -> None:
            if not text or not text.strip():
                return
            path = Path(text.strip()).expanduser()
            if path.is_dir():
                self.show_text("Not saved", f"{path} is a folder: give a file name.")
            elif path.exists():
                self.ask("Replace it?", f"{path} exists already. Replace it?", lambda: save(path))
            else:
                save(path)

        self.app.push_screen(InputScreen(title, str(start)), given)


class Notices(Static):
    """What a tab is missing, said plainly; hidden when nothing is."""

    DEFAULT_CSS = "Notices { height: auto; padding: 0 1; border-left: thick $warning; }"

    def __init__(self) -> None:
        super().__init__(markup=False)
        self.shown: list[str] = []
        self.display = False

    def set_messages(self, messages: list[str]) -> None:
        self.shown = list(messages)
        self.update(Text("\n".join(f"⚠ {m}" for m in messages), style=WARNING_COLOR))
        self.display = bool(messages)


class Line(Static):
    """A line of text (never markup), in a colour; hidden while empty."""

    DEFAULT_CSS = "Line { height: auto; }"

    def __init__(self, text: str = "", id: str | None = None, muted: bool = False) -> None:
        super().__init__(Text(text), id=id, markup=False, classes="muted" if muted else "")
        self.text = text
        self.display = bool(text)

    def set(self, text: str, color: str = "") -> None:
        self.text = text
        self.update(Text(text, style=color))
        self.display = bool(text)


class Activity(RichLog):
    """What a tab is doing, newest at the bottom."""

    DEFAULT_CSS = "Activity { height: 10; border: round $surface-lighten-2; }"

    def __init__(self, id: str | None = None) -> None:
        super().__init__(wrap=True, markup=False, id=id)
        self.border_title = "Activity"
        self.said: list[str] = []

    def say(self, text: str, error: bool = False) -> None:
        self.said.append(text)
        self.write(Text(f"⚠ {text}" if error else text, style=ERROR_COLOR if error else ""))


class FolderBox(Vertical):
    """A folder of the shared state: type a path (Enter) or Browse."""

    DEFAULT_CSS = """
    FolderBox { height: auto; border: round $primary; padding: 0 1; }
    FolderBox Horizontal { height: auto; }
    FolderBox Input { width: 1fr; }
    """

    def __init__(self, state: ToolboxState, key: str, title: str, tooltip: str) -> None:
        super().__init__(id=f"folder-{key}")
        self.state, self.key = state, key
        self.border_title = title
        self.tooltip = tooltip

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Input(placeholder="Not chosen yet: type a path and press Enter")
            yield Button("Browse", classes="browse")

    def on_mount(self) -> None:
        self.state.folder_changed.connect(lambda _key: self.refresh_folder())
        self.state.busy_changed.connect(self.refresh_folder)
        self.refresh_folder()

    def refresh_folder(self) -> None:
        folder = self.state.folder(self.key)
        self.query_one(Input).value = str(folder) if folder else ""
        locked = folder_locked(self.state, self.key)
        self.query_one(Input).disabled = locked
        self.query_one(Button).disabled = locked

    @on(Input.Submitted)
    def on_typed(self, event: Input.Submitted) -> None:
        event.stop()
        if event.value.strip():
            self.use(Path(event.value.strip()).expanduser())

    @on(Button.Pressed)
    def on_browse(self, event: Button.Pressed) -> None:
        event.stop()
        start = self.state.folder(self.key) or Path.home()
        screen = BrowseScreen(
            f"Choose: {self.border_title}", start if start.is_dir() else Path.home()
        )
        self.app.push_screen(screen, lambda chosen: self.use(chosen) if chosen else None)

    def use(self, folder: Path) -> None:
        folder = folder.absolute()
        if why := folder_conflict(self.state, self.key, folder):
            self.app.push_screen(TextScreen("Pick another folder", f"Not that folder: {why}."))
            self.refresh_folder()
            return
        if folder.is_dir():
            self.state.set_folder(self.key, folder)
        elif folder.exists() or self.key not in CREATED_IF_MISSING:
            self.app.push_screen(TextScreen("No such folder", f"There is no folder {folder}."))
            self.refresh_folder()
        else:
            question = f"There is no folder {folder} yet. Make it?"
            self.app.push_screen(
                ConfirmScreen("New folder", question), lambda a: self.make(folder, a)
            )

    def make(self, folder: Path, answer: str | None) -> None:
        if answer == "Yes":
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self.app.push_screen(TextScreen("Not made", str(e)))
            else:
                self.state.set_folder(self.key, folder)
        self.refresh_folder()


# ---- tables --------------------------------------------------------------------------------------


class Table(DataTable[Text]):
    """Rows by key; refilling keeps the highlighted row."""

    DEFAULT_CSS = "Table { height: auto; max-height: 18; }"

    def __init__(self, columns: Sequence[str], id: str | None = None) -> None:
        super().__init__(id=id, cursor_type="row", zebra_stripes=True)
        self.names = list(columns)
        self.row_keys: list[str] = []

    def on_mount(self) -> None:
        if not self.columns:
            self.set_columns(self.names)

    def set_columns(self, names: Sequence[str]) -> None:
        self.clear(columns=True)
        self.names, self.row_keys = list(names), []
        for name in names:
            self.add_column(cell(name))

    def fill(self, rows: Sequence[tuple[str, Sequence[object]]]) -> None:
        """[(key, cells)]; cells may be plain text or coloured Text."""
        if not self.columns:  # filled before it is mounted
            self.set_columns(self.names)
        current = self.current_key()
        self.clear()
        self.row_keys = [key for key, _cells in rows]
        for key, cells in rows:
            self.add_row(*(c if isinstance(c, Text) else cell(c) for c in cells), key=key)
        if current in self.row_keys:
            self.move_cursor(row=self.row_keys.index(current), animate=False)

    def current_key(self) -> str | None:
        row = self.cursor_row
        return self.row_keys[row] if 0 <= row < len(self.row_keys) else None

    def texts(self, key: str) -> list[str]:
        return [c.plain for c in self.get_row(key)]


class TickTable(Table):
    """A table whose rows can be ticked (space), in a first column of its own."""

    BINDINGS = [Binding("space", "tick", "Tick / untick")]

    class Ticked(Message):
        def __init__(self, table: "TickTable") -> None:
            super().__init__()
            self.table = table

        @property
        def control(self) -> "TickTable":
            return self.table

    def __init__(self, columns: Sequence[str], id: str | None = None) -> None:
        super().__init__(["", *columns], id=id)
        self.ticked: set[str] = set()
        self.locked = False

    def set_columns(self, names: Sequence[str]) -> None:
        super().set_columns(names if names and names[0] == "" else ["", *names])

    def fill(self, rows: Sequence[tuple[str, Sequence[object]]]) -> None:
        keys = [key for key, _cells in rows]
        self.ticked &= set(keys)
        super().fill([(key, (self.mark(key), *cells)) for key, cells in rows])

    def mark(self, key: str) -> Text:
        return cell(TICKED if key in self.ticked else UNTICKED)

    def ticked_keys(self) -> list[str]:
        return [key for key in self.row_keys if key in self.ticked]

    def action_tick(self) -> None:
        key = self.current_key()
        if key is not None and not self.locked:
            self.set_ticked({key} ^ self.ticked)

    def set_ticked(self, keys: set[str]) -> None:
        self.ticked = keys & set(self.row_keys)
        for row, key in enumerate(self.row_keys):
            self.update_cell_at((row, 0), self.mark(key))  # pyright: ignore[reportArgumentType]
        self.post_message(self.Ticked(self))

    def tick_all(self, on: bool) -> None:
        self.set_ticked(set(self.row_keys) if on else set())


class FilledSelect(Select[str]):
    """A dropdown refilled without losing (or announcing) the choice."""

    def fill(self, options: Sequence[tuple[str, str]], prefer: str | None = None) -> str | None:
        """[(text, value)]; the value now chosen (the old one if still there, else the first)."""
        values = [value for _text, value in options]
        old = option_text(self.value)
        chosen = next((v for v in (prefer, old) if v in values), values[0] if values else None)
        with self.prevent(Select.Changed):
            self.set_options([(text, value) for text, value in options])
            if chosen is not None:
                self.value = chosen
        return chosen

    def chosen(self) -> str | None:
        return option_text(self.value)


# ---- Docker --------------------------------------------------------------------------------------


class DockerLine(Line):
    """Docker's state (the owner asks Docker and calls show_state)."""

    def show_state(self, running: bool | None, error: str = "", warning: str = "") -> None:
        self.set(*docker_line(running, error, warning))


class ImageBox(Horizontal):
    """A Docker image the tab needs: download / continue / remove it."""

    DEFAULT_CSS = """
    ImageBox { height: auto; }
    ImageBox ProgressBar { width: 40; }
    """

    class Finished(Message):
        def __init__(self, box: "ImageBox", text: str, error: bool) -> None:
            super().__init__()
            self.box, self.text, self.error = box, text, error

    def __init__(
        self, image: str, what: str, size_text: str, settings: Settings, remove_note: str = ""
    ) -> None:
        super().__init__()
        self.info = ImageState(image, what, size_text, remove_note, settings)
        self.busy = False
        self.lock_reason = ""

    def compose(self) -> ComposeResult:
        yield Button("", id="download", variant="success")
        yield Button("Remove partial download", id="remove-partial", variant="error")
        yield Button("", id="remove")
        yield ProgressBar(total=100, show_eta=False)
        yield Line(id="image-text", muted=True)

    def on_mount(self) -> None:
        self.query_one("#remove", Button).label = f"Remove {self.info.what}"
        self.query_one(ProgressBar).display = False
        self.update_buttons()

    def set_present(self, present: bool | None) -> None:
        """Whether the image is on this computer (None: unknown, Docker not running)."""
        self.info.present = present
        if present and self.info.partial and not self.busy:
            self.info.set_partial("")
        self.update_buttons()

    def update_buttons(self) -> None:
        present = self.info.present
        self.display = present is not None or self.busy
        download = self.query_one("#download", Button)
        download.label = self.info.download_text()
        download.display = present is False and not self.busy
        self.query_one("#remove-partial").display = (
            present is False and bool(self.info.partial) and not self.busy
        )
        button = self.query_one("#remove", Button)
        button.display = present is True and not self.busy
        button.disabled = bool(self.lock_reason)

    def page(self) -> Page:
        return next(a for a in self.ancestors if isinstance(a, Page))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        page = self.page()
        if event.button.id == "download":
            self.download()
        elif event.button.id == "remove":
            question = self.info.remove_question()
            page.ask(f"Remove {self.info.what}?", question, lambda: self.remove_it(False))
        else:
            question = self.info.remove_partial_question()
            page.ask("Remove the partial download?", question, lambda: self.remove_it(True))

    def download(self) -> None:
        if self.busy:
            return
        self.busy = True
        if not self.info.partial:  # remembered: closing mid-way leaves it partial
            self.info.set_partial("0% done")
        page = self.page()
        job = PullJob(self.info.image, lambda f, t: page.tell(self.on_progress, f, t))
        self.query_one(ProgressBar).display = True
        self.query_one("#image-text", Line).set("Asking the registry how big it is...")
        page.start_job(job.run, self.on_downloaded, self.on_downloaded)
        self.update_buttons()

    def on_progress(self, fraction: float, text: str) -> None:
        self.query_one(ProgressBar).update(total=100, progress=max(fraction, 0) * 100)
        self.query_one("#image-text", Line).set(text)
        if fraction > 0:
            self.info.set_partial(f"{fraction:.0%} done")

    def on_downloaded(self, error: str) -> None:
        self.busy = False
        self.query_one(ProgressBar).display = False
        if error:
            text = f"⚠ Download stopped: {error}"
        else:
            text = f"{self.info.what} downloaded."
            self.info.present = True
            self.info.set_partial("")
        self.query_one("#image-text", Line).set(text)
        self.update_buttons()
        self.post_message(self.Finished(self, text, bool(error)))

    def remove_it(self, partial: bool) -> None:
        self.busy = True
        self.query_one("#image-text", Line).set("Removing...")
        image = self.info.image
        page = self.page()
        page.start_job(lambda: remove(image, partial), self.on_removed, self.on_removed)
        self.update_buttons()

    def on_removed(self, error: str) -> None:
        self.busy = False
        if error:
            text = f"⚠ Not removed: {error}"
        else:
            text = f"{self.info.what} removed."
            self.info.present = False
            self.info.set_partial("")
        self.query_one("#image-text", Line).set(text)
        self.update_buttons()
        self.post_message(self.Finished(self, text, bool(error)))


class RunBox(Vertical):
    """A running batch: its title, step bar and subject bar (the owner adds stop buttons)."""

    DEFAULT_CSS = """
    RunBox { height: auto; border: round $success; padding: 0 1; }
    RunBox ProgressBar { width: 100%; }
    RunBox Horizontal { height: auto; }
    """

    def __init__(self, title: str, buttons: Sequence[tuple[str, str]]) -> None:
        super().__init__()
        self.border_title = title
        self.buttons = buttons  # (id, text)
        self.progress = BatchProgress()
        self.spinner = 0

    def compose(self) -> ComposeResult:
        yield Line(id="run-title")
        yield ProgressBar(id="run-steps", show_eta=False)
        yield Line(id="run-details", muted=True)
        yield Line(id="run-subjects", muted=True)
        with Horizontal():
            for button_id, text in self.buttons:
                yield Button(text, id=button_id, variant="warning")

    def on_mount(self) -> None:
        self.display = False
        self.timer = self.set_interval(SPINNER_S, self.spin, pause=True)

    def start(self, subjects: int, estimate: int | None, stage: str) -> None:
        self.progress.start(subjects, estimate, stage)
        self.display = True
        self.timer.resume()
        self.show_progress()

    def set_stage(self, stage: str) -> None:
        self.progress.stage = stage
        self.show_progress()

    def finish(self) -> None:
        self.progress.running.clear()
        self.timer.pause()
        self.display = False

    def spin(self) -> None:
        self.spinner = (self.spinner + 1) % len(SPINNER)
        self.show_progress()

    def show_progress(self) -> None:
        batch = self.progress
        self.query_one("#run-title", Line).set(f"{SPINNER[self.spinner]}  {batch.title()}")
        bar = self.query_one("#run-steps", ProgressBar)
        mean = batch.mean_steps()
        if batch.estimate and batch.running:
            bar.update(total=max(batch.estimate, mean + 1), progress=mean)
        else:
            bar.update(total=None)  # moving: nothing to measure against yet
        self.query_one("#run-details", Line).set(batch.details())
        self.query_one("#run-subjects", Line).set(f"Subjects: {batch.done} of {batch.total} done")
