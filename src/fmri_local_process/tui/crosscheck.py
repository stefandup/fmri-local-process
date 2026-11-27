"""BIDS Crosscheck: the sessions, and the highlighted one's problems, files and repeated runs."""

from collections.abc import Callable
from functools import partial

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Input, Static

from fmri_local_process.app.crosscheck import (
    COLORS,
    CROSSCHECKED_COLOR,
    CROSSCHECKED_ICON,
    HELP_TEXT,
    ICONS,
    TABLE_COLUMNS,
    badge_text,
    delete_question,
    delete_sessions,
    inventory_detail,
    inventory_notices,
    mark_text,
    note_text,
    problem_color,
    review_cells,
    scan_inventory_text,
    scanned_text,
    summary_text,
)
from fmri_local_process.app.notices import bids_notices
from fmri_local_process.app.state import ToolboxState
from fmri_local_process.app.tabs import CROSSCHECK
from fmri_local_process.processing.crosscheck import (
    CrosscheckError,
    DataFile,
    SessionReview,
    delete_data_file,
    keep_run,
    review_bids_folder,
    set_crosschecked,
    set_note,
    sidecar_note,
)
from fmri_local_process.tui.trimming import start_trimming
from fmri_local_process.tui.widgets import (
    Activity,
    InputScreen,
    Line,
    Notices,
    Page,
    Table,
    TickTable,
    cell,
)


class CrosscheckPage(Page):
    GUIDE = HELP_TEXT

    def __init__(self, state: ToolboxState) -> None:
        super().__init__(state)
        self.seen: tuple = ()
        self.reviews: list[SessionReview] = []
        self.noted: SessionReview | None = None  # whose note the box shows

    def compose(self) -> ComposeResult:
        yield Notices()
        yield Line(id="lock")
        with Horizontal(classes="row"):
            yield Button("Select all", id="all")
            yield Button("Select none", id="none")
            yield Button("Rescan", id="rescan")
        yield TickTable(TABLE_COLUMNS, id="sessions")
        yield Line(id="summary", muted=True)
        yield Line(id="ticked")
        with Horizontal(classes="row"):
            yield Button("Mark ticked crosschecked", id="mark-ticked", variant="success")
            yield Button("Un-mark ticked", id="unmark-ticked")
            yield Button("Note...", id="note-ticked")
            yield Button("Delete sessions...", id="delete-ticked", variant="error")
        yield Line(id="title")
        yield Line(id="inventory")
        with Horizontal(classes="row"):
            yield Button("Mark crosschecked", id="mark", variant="success")
            yield Button("Un-mark crosschecked", id="unmark")
            yield Button("Delete session...", id="delete", variant="error")
            yield Button("Inventory note...", id="inventory-note")
        with Horizontal(classes="row"):
            yield Static("Note:", classes="label")
            yield Input(
                placeholder="e.g. scan stopped early (Enter saves)", id="note", classes="wide"
            )
        yield Static("Problems", classes="section")
        yield Table(["", "Problem"], id="issues")
        yield Static("Repeated runs: keep one", classes="section", id="pending-title")
        yield Table(["Scan", "Run", "What", "Note"], id="pending")
        with Horizontal(classes="row", id="pending-buttons"):
            yield Button("Keep highlighted run, delete the others", id="keep")
        yield Static("Files", classes="section")
        yield Table(["", "File", "What", "Trim to"], id="files")
        with Horizontal(classes="row"):
            yield Button("Trim highlighted...", id="trim")
            yield Button("Delete highlighted...", id="delete-file")
        yield Activity()

    def on_mount(self) -> None:
        self.state.busy_changed.connect(self.show_session)

    @property
    def activity(self) -> Activity:
        return self.query_one(Activity)

    @property
    def table(self) -> TickTable:
        return self.query_one("#sessions", TickTable)

    def show_page(self) -> None:
        if self.seen != (self.state.generation, self.state.folder("bids")):
            self.rescan()

    @on(Button.Pressed, "#rescan")
    def rescan(self) -> None:
        bids = self.state.folder("bids")
        self.seen = (self.state.generation, bids)
        self.reviews = review_bids_folder(bids) if bids else []
        self.query_one(Notices).set_messages(
            bids_notices(bids, bool(self.reviews)) + inventory_notices(bids, self.reviews)
        )
        rows = []
        for review in self.reviews:
            subject, day, session, files, problems, note, inventory = review_cells(review)
            coloured = cell(problems, problem_color(review))
            rows.append((review.name, (subject, day, session, files, coloured, note, inventory)))
        self.table.fill(rows)
        self.query_one("#summary", Line).set(summary_text(self.reviews, bids))
        self.toolbox.set_badge(CROSSCHECK, badge_text(self.reviews))
        self.show_session()

    # ---- what is chosen -----------------------------------------------------------------------

    def current(self) -> SessionReview | None:
        key = self.table.current_key()
        return next((r for r in self.reviews if r.name == key), None)

    def ticked(self) -> list[SessionReview]:
        return [r for r in self.reviews if r.name in self.table.ticked]

    def pending_files(self, review: SessionReview) -> list[DataFile]:
        return sorted(review.pending, key=lambda d: (d.label, d.path.name))

    @on(TickTable.Ticked)
    def update_ticked(self) -> None:
        ticked = self.ticked()
        n, locked = len(ticked), bool(self.state.lock_reason())
        self.query_one("#ticked", Line).set(
            f"{n} ticked" if n else "Tick sessions (space) to act on several at once."
        )
        self.query_one("#note-ticked", Button).disabled = n == 0
        self.query_one("#delete-ticked", Button).disabled = n == 0 or locked
        self.query_one("#mark-ticked", Button).disabled = all(r.crosschecked for r in ticked)
        self.query_one("#unmark-ticked", Button).disabled = not any(r.crosschecked for r in ticked)

    @on(Table.RowHighlighted, "#sessions")
    def show_session(self) -> None:
        reason = self.state.lock_reason()
        self.query_one("#lock", Line).set(f"Locked: {reason}." if reason else "", "#f39c12")
        self.update_ticked()
        review = self.current()
        note = self.query_one("#note", Input)
        if review is None or review is not self.noted or not note.has_focus:
            self.noted = review
            note.disabled = review is None
            note.value = review.note if review else ""
        issues, pending, files = (
            self.query_one(f"#{i}", Table) for i in ("issues", "pending", "files")
        )
        title = self.query_one("#title", Line)
        if review is None:
            title.set("Select a session.")
        elif review.crosschecked:
            title.set(
                f"{CROSSCHECKED_ICON} {review.name}: crosschecked, {scanned_text(review)}",
                CROSSCHECKED_COLOR,
            )
        else:
            title.set(f"{review.name}: {scanned_text(review)}")
        inventory = scan_inventory_text(review.inventory) if review else ""
        self.query_one("#inventory", Line).set(inventory)
        no_row = review is None or review.inventory is None
        self.query_one("#inventory-note", Button).disabled = no_row
        crosschecked = review is not None and review.crosschecked
        self.query_one("#mark").display = not crosschecked
        self.query_one("#unmark").display = crosschecked
        self.query_one("#mark", Button).disabled = review is None or not review.source_folder
        self.query_one("#delete", Button).disabled = (
            review is None or review.session is None or bool(reason)
        )
        found = review.issues if review else []
        issues.fill(
            [(str(i), (cell(ICONS[x.kind], COLORS[x.kind]), x.text)) for i, x in enumerate(found)]
        )
        candidates = self.pending_files(review) if review else []
        pending.fill(
            [
                (str(i), (d.label, d.path.name, d.summary(), sidecar_note(d)))
                for i, d in enumerate(candidates)
            ]
        )
        for widget_id in ("#pending", "#pending-title", "#pending-buttons"):
            self.query_one(widget_id).display = bool(candidates)
        self.query_one("#keep", Button).disabled = bool(reason)
        rows = []
        for i, data in enumerate(review.files if review else []):
            kind, trim_to = self.flag(review, data)
            icon = cell(ICONS[kind] if kind else "✓", COLORS[kind or "ok"])
            rows.append(
                (str(i), (icon, f"{data.datatype}/{data.path.name}", data.summary(), trim_to))
            )
        files.fill(rows)
        self.update_file_buttons()

    @staticmethod
    def flag(review: SessionReview | None, data: DataFile) -> tuple[str, int | str]:
        """(the kind of length problem, '' for none; what to trim it to, '' for nothing)"""
        for issue in review.issues if review else []:
            if issue.path == data.path and issue.kind in ("short", "long"):
                return issue.kind, issue.trim_to or ""
        return "", ""

    def current_file(self) -> DataFile | None:
        review, key = self.current(), self.query_one("#files", Table).current_key()
        if review is None or key is None or int(key) >= len(review.files):
            return None
        return review.files[int(key)]

    @on(Table.RowHighlighted, "#files")
    def update_file_buttons(self) -> None:
        data, locked = self.current_file(), bool(self.state.lock_reason())
        trim_to = self.flag(self.current(), data)[1] if data else ""
        self.query_one("#trim", Button).disabled = locked or not trim_to
        self.query_one("#delete-file", Button).disabled = locked or data is None

    # ---- actions ------------------------------------------------------------------------------

    def act(self, description: str, action: Callable[[], object]) -> None:
        try:
            action()
        except (CrosscheckError, OSError) as e:
            self.activity.say(f"ERROR  {description}: {e}", error=True)
            self.show_text("Not done", str(e))
            return
        self.activity.say(description)
        self.state.bump()

    def mark_reviews(self, reviews: list[SessionReview], marked: bool) -> None:
        bids = self.state.folder("bids")
        if bids is not None and reviews:
            action = partial(set_crosschecked, bids, reviews, marked)
            self.act(mark_text(reviews, marked), action)

    def current_list(self) -> list[SessionReview]:
        review = self.current()
        return [review] if review else []

    def save_note(self, reviews: list[SessionReview], text: str) -> None:
        bids = self.state.folder("bids")
        if bids is not None and reviews:
            self.act(note_text(reviews, text), partial(set_note, bids, reviews, text))

    @on(Input.Submitted, "#note")
    @on(Input.Blurred, "#note")
    def on_note(self, event: Input.Submitted | Input.Blurred) -> None:
        """Saved with Enter, and when the box is left."""
        review = self.noted
        if review is not None and event.value.strip() != review.note:
            self.save_note([review], event.value)

    def delete_reviews(self, reviews: list[SessionReview]) -> None:
        bids = self.state.folder("bids")
        reviews = [r for r in reviews if r.session is not None]
        if bids is None or not reviews:
            return

        def delete() -> None:
            if reason := self.state.lock_reason():
                self.show_text("Not now", f"Can't delete now: {reason}.")
                return
            _deleted, errors = delete_sessions(bids, reviews, self.activity.say)
            if errors:
                self.show_text("Not deleted", "\n".join(errors))
            self.state.bump()

        self.ask("Confirm", delete_question(reviews), delete)

    @on(Button.Pressed, "#keep")
    def keep(self) -> None:
        review, bids = self.current(), self.state.folder("bids")
        key = self.query_one("#pending", Table).current_key()
        if review is None or bids is None or key is None:
            return
        chosen = self.pending_files(review)[int(key)]
        candidates = [d for d in review.pending if d.label == chosen.label]
        self.ask(
            "Confirm",
            f"Keep {chosen.path.name} and delete the {len(candidates) - 1} other run(s)?",
            lambda: self.act(
                f"Kept {chosen.path.name}", partial(keep_run, bids, chosen, candidates)
            ),
        )

    @on(Button.Pressed, "#trim")
    def trim(self) -> None:
        data = self.current_file()
        trim_to = self.flag(self.current(), data)[1] if data else ""
        if data is not None and isinstance(trim_to, int):
            start_trimming(self, [(data, trim_to)], self.activity.say)

    @on(Button.Pressed, "#delete-file")
    def delete_file(self) -> None:
        data, bids = self.current_file(), self.state.folder("bids")
        if data is not None and bids is not None:
            self.ask(
                "Confirm",
                f"Delete {data.path.name} and its sidecar from the BIDS folder?",
                lambda: self.act(
                    f"Deleted {data.path.name}", partial(delete_data_file, bids, data)
                ),
            )

    @on(Button.Pressed)
    def on_session_button(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "all" | "none":
                self.table.tick_all(event.button.id == "all")
            case "mark" | "unmark":
                self.mark_reviews(self.current_list(), event.button.id == "mark")
            case "mark-ticked" | "unmark-ticked":
                self.mark_reviews(self.ticked(), event.button.id == "mark-ticked")
            case "delete":
                self.delete_reviews(self.current_list())
            case "delete-ticked":
                self.delete_reviews(self.ticked())
            case "note-ticked":
                self.note_ticked()
            case "inventory-note":
                review = self.current()
                if review is not None and review.inventory is not None:
                    self.show_text(
                        f"{review.name}: scan inventory", inventory_detail(review.inventory)
                    )

    def note_ticked(self) -> None:
        reviews = self.ticked()
        notes = {r.note for r in reviews}
        title = f"Note for {len(reviews)} ticked session(s) (empty clears it):"
        start = notes.pop() if len(notes) == 1 else ""

        def given(text: str | None) -> None:
            if text is not None:
                self.save_note(reviews, text)

        self.app.push_screen(InputScreen(title, start), given)
