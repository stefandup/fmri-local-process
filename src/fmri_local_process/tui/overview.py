"""Crosscheck overview: the whole BIDS folder at a glance, and the action log."""

from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Checkbox, Input, Static

from fmri_local_process.app.crosscheck import CROSSCHECKED_COLOR, needs_a_look
from fmri_local_process.app.notices import bids_notices
from fmri_local_process.app.overview import (
    CELL_COLORS,
    CELL_LEGEND,
    PROBLEMS_LEGEND,
    matches_search,
    problem_summary,
    summary_lines,
)
from fmri_local_process.app.state import ToolboxState
from fmri_local_process.processing.action_log import COLUMNS, read_actions
from fmri_local_process.processing.crosscheck import (
    SessionReview,
    overview_columns,
    overview_row,
    overview_tasks,
    read_expected_sessions,
    review_bids_folder,
    set_note,
    write_overview_csv,
)
from fmri_local_process.processing.philani_bids import read_excluded_sessions
from fmri_local_process.tui.widgets import Line, Notices, Page, Table, cell, on_background


class OverviewPage(Page):
    GUIDE = f"Colours\n{CELL_LEGEND}\n\nProblems\n{PROBLEMS_LEGEND}"

    def __init__(self, state: ToolboxState) -> None:
        super().__init__(state)
        self.seen: tuple = ()
        self.reviews: list[SessionReview] = []
        self.logged: list[dict[str, str]] = []
        self.noted: SessionReview | None = None  # whose note the box shows

    def compose(self) -> ComposeResult:
        yield Notices()
        yield Line(id="summary")
        with Horizontal(classes="row"):
            yield Checkbox("Only sessions with something to check", id="problems-only")
            yield Button("Refresh", id="refresh")
            yield Button("Export CSV...", id="export")
        yield Table([], id="sessions")
        with Horizontal(classes="row"):
            yield Static("Note:", classes="label")
            yield Input(
                placeholder="The highlighted session's note (Enter saves)",
                id="note",
                classes="wide",
            )
        yield Line(id="saved", muted=True)
        yield Static("Action log", classes="section")
        yield Input(placeholder="Search: subject, action, file, user...", id="search")
        yield Table([c.capitalize() for c in COLUMNS], id="actions")

    def show_page(self) -> None:
        if self.seen != (self.state.generation, self.state.folder("bids")):
            self.refresh_reviews()

    @on(Button.Pressed, "#refresh")
    def refresh_reviews(self) -> None:
        bids = self.state.folder("bids")
        self.seen = (self.state.generation, bids)
        if bids is None:
            self.reviews, self.logged = [], []
            lines = ["Choose the BIDS folder on the Setup & Convert tab."]
        else:
            self.reviews = [r for r in review_bids_folder(bids) if r.session is not None]
            excluded = len(read_excluded_sessions(bids))
            lines = summary_lines(self.reviews, read_expected_sessions(bids), excluded)
            self.logged = list(reversed(read_actions(bids)))
        self.query_one("#summary", Line).set("\n".join(lines))
        self.query_one(Notices).set_messages(bids_notices(bids, bool(self.reviews)))
        self.query_one("#export", Button).disabled = not self.reviews
        self.fill_sessions()
        self.fill_actions()

    @on(Checkbox.Changed, "#problems-only")
    def fill_sessions(self) -> None:
        tasks = overview_tasks(self.reviews)
        only = self.query_one("#problems-only", Checkbox).value
        table = self.query_one("#sessions", Table)
        columns = [*overview_columns(tasks), "Problems", "Note"]
        if table.names != columns:
            table.set_columns(columns)
        rows = []
        for review in self.reviews:
            if only and not needs_a_look(review):
                continue
            cells = [
                on_background(c.text, CELL_COLORS[c.status]) if c.status else cell(c.text)
                for c in overview_row(review, tasks)
            ]
            problems = problem_summary(review)
            if needs_a_look(review):
                cells.append(on_background(problems, CELL_COLORS["missing"]))
            else:
                cells.append(cell(problems, CROSSCHECKED_COLOR if review.crosschecked else ""))
            rows.append((review.name, (*cells, review.note)))
        table.fill(rows)
        self.show_note()

    def current(self) -> SessionReview | None:
        key = self.query_one("#sessions", Table).current_key()
        return next((r for r in self.reviews if r.name == key), None)

    @on(Table.RowHighlighted, "#sessions")
    def show_note(self) -> None:
        review = self.noted = self.current()
        note = self.query_one("#note", Input)
        note.disabled = review is None
        note.value = review.note if review else ""
        note.border_title = review.name if review else ""

    @on(Input.Submitted, "#note")
    @on(Input.Blurred, "#note")
    def save_note(self, event: Input.Submitted | Input.Blurred) -> None:
        review, bids = self.noted, self.state.folder("bids")
        if review is None or bids is None or event.value.strip() == review.note:
            return
        try:
            set_note(bids, [review], event.value)
        except OSError as e:
            self.show_text("Note not saved", str(e))
            return
        self.query_one("#saved", Line).set(f"Note of {review.name} saved")
        self.state.bump()  # refreshes this table too

    @on(Input.Changed, "#search")
    def fill_actions(self) -> None:
        search = self.query_one("#search", Input).value
        rows = [
            (str(i), [action.get(c, "") or "" for c in COLUMNS])
            for i, action in enumerate(self.logged)
            if matches_search(action, search)
        ]
        self.query_one("#actions", Table).fill(rows)

    @on(Button.Pressed, "#export")
    def export(self) -> None:
        bids = self.state.folder("bids")
        if bids is None or not self.reviews:
            return

        def write(path: Path) -> None:
            try:
                write_overview_csv(self.reviews, path)
            except OSError as e:
                self.show_text("Not exported", str(e))
                return
            self.show_text("Exported", f"{len(self.reviews)} session(s) saved to {path}")

        start = bids.parent / f"{bids.name}_overview.csv"
        self.ask_save_path("Export CSV to:", start, write)
