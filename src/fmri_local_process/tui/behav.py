"""Raw DICOMs and Behaviour logs: two tabs on one read of the logs and DICOM folders."""

import datetime as dt

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Checkbox, Input, ProgressBar, Static

from fmri_local_process.app.behav import (
    COLUMNS,
    EXPORT_INTO_BIDS_QUESTION,
    EXPORT_LAYOUTS,
    EXPORT_SUBFOLDER,
    HELP_TEXT,
    INTO_BIDS,
    INTO_OUTPUT,
    MATCH,
    NOTES,
    S_CHECKED,
    S_LOGS,
    S_NOTES,
    S_RESOLVED,
    S_SESSION,
    SESSION,
    SESSION_COLUMNS,
    SESSIONS_HELP_TEXT,
    STATE,
    VERDICT,
    VERDICT_COLORS,
    BehavData,
    BehavWork,
    behav_folders,
    event_rows,
    export_text,
    folder_name_parts,
    inventory_text,
    load_behav_data,
    log_cells,
    log_title,
    missing_folders_notice,
    parse_date,
    parse_session,
)
from fmri_local_process.app.colors import ERROR_COLOR, OK_COLOR, WARNING_COLOR
from fmri_local_process.app.crosscheck import CROSSCHECKED_COLOR, CROSSCHECKED_ICON
from fmri_local_process.app.events import Event
from fmri_local_process.app.state import VIEW_ONLY, ToolboxState
from fmri_local_process.app.tabs import BEHAV
from fmri_local_process.processing.behav_export import BIDS_LAYOUT
from fmri_local_process.processing.behav_logs import EventTable
from fmri_local_process.processing.behav_match import (
    COMPLETE,
    ISO_DATE,
    MATCHED,
    REVIEW_ERROR,
    REVIEW_OK,
    LogInfo,
    LogMatch,
    events_summary,
    needs_review,
    same_day_rivals,
)
from fmri_local_process.tui.widgets import (
    BrowseScreen,
    Line,
    Notices,
    Page,
    PickScreen,
    Table,
    TickTable,
    cell,
)

MAX_EVENT_ROWS = 500  # shown; an events file is far shorter


def date_text(day: dt.date | None) -> str:
    return day.strftime(ISO_DATE) if day else ""


class BehavShared:
    """What both tabs show: the logs read once, with the corrections given so far."""

    def __init__(self, state: ToolboxState) -> None:
        self.state = state
        self.changed = Event()  # read again, re-checked, saved or dropped
        self.progress = Event()  # done, total, what
        self.work: BehavWork | None = None
        self.loading = False
        self.reload_wanted = False  # a folder changed while reading
        self.error = ""
        self.unsaved = False

    def infos(self) -> list[LogInfo]:
        return self.work.infos if self.work else []

    def load(self, page: Page) -> None:
        """Read the logs and DICOM folders in the background."""
        folders = behav_folders(self.state)
        if folders is None:
            return
        if self.loading:
            self.reload_wanted = True
            return
        self.work, self.loading, self.error, self.unsaved = None, True, "", False
        self.changed.emit()

        def progress(done: int, total: int, what: str) -> None:
            page.tell(self.progress.emit, done, total, what)

        def read() -> BehavData | str:
            try:
                return load_behav_data(*folders, progress=progress)
            except (OSError, ValueError) as e:
                return str(e)

        def loaded(data: BehavData | str) -> None:
            self.loading = False
            if self.reload_wanted:
                self.reload_wanted = False
                self.load(page)
                return
            if isinstance(data, str):
                self.error = f"Can't read the logs: {data}"
            else:
                self.work = BehavWork(*folders, data)
                self.work.output_root = self.state.folder("output")
                self.work.bids_locked = VIEW_ONLY in self.state.busy
            self.changed.emit()

        page.start_job(read, loaded, loaded)

    def recheck(self) -> None:
        if self.work is not None:
            self.work.match()
            self.unsaved = True
            self.changed.emit()

    def save(self) -> None:
        if self.work is not None and not self.work.bids_locked:
            self.work.save()
            self.unsaved = False
            self.changed.emit()

    def notices(self) -> list[str]:
        if missing := missing_folders_notice(self.state):
            return missing
        messages = [self.error] if self.error else []
        if self.work is not None:
            messages += self.work.notices()
        return messages


class SharedPage(Page):
    """The reading, Re-check and Save both tabs have."""

    def __init__(self, state: ToolboxState) -> None:
        super().__init__(state)
        self.filled = False

    @property
    def shared(self) -> BehavShared:
        return self.toolbox.behav_shared

    @property
    def work(self) -> BehavWork | None:
        return self.shared.work

    def compose_controls(self) -> ComposeResult:
        yield Notices()
        with Horizontal(classes="row", id="loading"):
            yield ProgressBar(show_eta=False)
            yield Line(id="loading-what", muted=True)
        with Horizontal(classes="row"):
            yield Button("Reload", id="reload")
            yield Button("Save", id="save", variant="primary")
        yield Line(id="status")

    def on_mount(self) -> None:
        self.query_one("#loading").display = False
        self.shared.changed.connect(self.on_shared_changed)
        self.shared.progress.connect(self.on_progress)
        self.state.folder_changed.connect(self.on_folder_changed)
        self.on_shared_changed()

    def on_folder_changed(self, key: str) -> None:
        if key == "output" and self.work is not None:
            self.work.output_root = self.state.folder("output")

    def show_page(self) -> None:
        if self.work is None and not self.shared.error:
            self.shared.load(self)

    def on_progress(self, done: int, total: int, what: str) -> None:
        self.query_one("#loading ProgressBar", ProgressBar).update(
            total=max(total, 1), progress=done
        )
        self.query_one("#loading-what", Line).set(what)

    def on_shared_changed(self) -> None:
        loading = self.shared.loading
        self.query_one("#loading").display = loading
        self.query_one(Notices).set_messages(self.shared.notices())
        ready = behav_folders(self.state) is not None
        self.query_one("#reload", Button).disabled = loading or not ready
        locked = self.work is None or self.work.bids_locked
        self.query_one("#save", Button).disabled = locked
        status = self.query_one("#status", Line)
        if loading:
            status.set("Reading the DICOM folders and logs...")
        elif self.work is None:
            status.set("")
        else:
            unsaved = "   (changes not saved yet)" if self.shared.unsaved else ""
            status.set(self.work.summary() + unsaved, WARNING_COLOR if unsaved else "")
        self.fill()

    def fill(self) -> None: ...

    @on(Button.Pressed, "#reload")
    def reload(self) -> None:
        if self.shared.unsaved:
            question = "Corrections not saved yet are dropped. Read everything again?"
            self.ask("Reload", question, lambda: self.shared.load(self))
        else:
            self.shared.load(self)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        try:
            self.shared.save()
        except OSError as e:
            self.show_text("Not saved", str(e))
            return
        self.show_text("Saved", "Corrections saved. Convert to BIDS (tab 1) to apply them.")


class SessionsPage(SharedPage):
    """Raw DICOMs: every DICOM session folder, with its corrections."""

    GUIDE = SESSIONS_HELP_TEXT

    def compose(self) -> ComposeResult:
        yield from self.compose_controls()
        yield TickTable(SESSION_COLUMNS, id="folders")
        yield Line(id="picked")
        with Horizontal(classes="row"):
            yield Static("Corrected ID:", classes="label")
            yield Input(id="folder-id")
            yield Static("Corrected date:", classes="label")
            yield Input(placeholder="YYYY-MM-DD", id="folder-date", max_length=10)
            yield Static("Set session:", classes="label")
            yield Input(placeholder="by date", id="folder-session", max_length=2)
            yield Button("Use", id="use")
        yield Line(id="problem")
        yield Line(id="ticked")
        with Horizontal(classes="row"):
            yield Button("Mark ticked crosschecked", id="mark", variant="success")
            yield Button("Un-mark ticked", id="unmark")

    def fill(self) -> None:
        work = self.work
        rows = []
        for row in range(len(work.scanned) if work else 0):
            assert work is not None
            folder, tasks = work.scanned[row]
            when, raw_id = folder_name_parts(folder.name)
            fixes, cells = work.fixes, work.folder_cells(row)
            rows.append(
                (
                    folder.name,
                    (
                        folder.name,
                        when,
                        raw_id,
                        fixes.ids.get(folder.name, ""),
                        date_text(fixes.dates.get(folder.name)),
                        fixes.sessions.get(folder.name, ""),
                        cells[S_RESOLVED],
                        cells[S_SESSION],
                        ", ".join(sorted(tasks)),
                        cells[S_LOGS],
                        cell(cells[S_CHECKED], CROSSCHECKED_COLOR),
                        cell(cells[S_NOTES], WARNING_COLOR),
                    ),
                )
            )
        self.query_one("#folders", TickTable).fill(rows)
        self.show_folder()
        self.update_ticked()

    def folder(self) -> str | None:
        return self.query_one("#folders", TickTable).current_key()

    @on(Table.RowHighlighted, "#folders")
    def show_folder(self) -> None:
        name, work = self.folder(), self.work
        for widget_id in ("#folder-id", "#folder-date", "#folder-session", "#use"):
            self.query_one(widget_id).disabled = name is None
        self.query_one("#problem", Line).set("")
        if name is None or work is None:
            self.query_one("#picked", Line).set("")
            return
        row = [f.name for f, _tasks in work.scanned].index(name)
        self.query_one("#picked", Line).set(f"{name}   {work.folder_cells(row)[S_NOTES]}")
        fixes = work.fixes
        self.query_one("#folder-id", Input).value = fixes.ids.get(name, "")
        self.query_one("#folder-date", Input).value = date_text(fixes.dates.get(name))
        self.query_one("#folder-session", Input).value = fixes.sessions.get(name, "")

    @on(Input.Submitted)
    @on(Button.Pressed, "#use")
    def use_typed(self) -> None:
        """The highlighted folder's corrections, as typed: matched again, not saved yet."""
        name, work = self.folder(), self.work
        if name is None or work is None:
            return
        try:
            day = parse_date(self.query_one("#folder-date", Input).value)
            session = parse_session(self.query_one("#folder-session", Input).value)
        except ValueError as e:
            self.query_one("#problem", Line).set(str(e), ERROR_COLOR)
            return
        subject = self.query_one("#folder-id", Input).value.strip()
        fixes = work.fixes
        for values, value in ((fixes.ids, subject), (fixes.dates, day), (fixes.sessions, session)):
            if value:
                values[name] = value  # pyright: ignore[reportArgumentType] -- each its own type
            else:
                values.pop(name, None)
        self.shared.recheck()

    @on(TickTable.Ticked)
    def update_ticked(self) -> None:
        work = self.work
        ticked = self.query_one("#folders", TickTable).ticked
        checked = work.fixes.checked if work else set()
        total = len(work.scanned) if work else 0
        done = len(checked & {f.name for f, _t in work.scanned}) if work else 0
        self.query_one("#ticked", Line).set(
            f"{len(ticked)} ticked (space); {CROSSCHECKED_ICON} {done} of {total} crosschecked"
        )
        self.query_one("#mark", Button).disabled = not ticked - checked
        self.query_one("#unmark", Button).disabled = not ticked & checked

    @on(Button.Pressed, "#mark")
    def mark(self) -> None:
        self.set_checked(True)

    @on(Button.Pressed, "#unmark")
    def unmark(self) -> None:
        self.set_checked(False)

    def set_checked(self, marked: bool) -> None:
        if self.work is None:
            return
        ticked = self.query_one("#folders", TickTable).ticked
        if marked:
            self.work.fixes.checked |= ticked
        else:
            self.work.fixes.checked -= ticked
        self.shared.recheck()


class BehavPage(SharedPage):
    """Behaviour logs: match each log to a session, look at its events, give a verdict."""

    GUIDE = HELP_TEXT

    def compose(self) -> ComposeResult:
        yield from self.compose_controls()
        with Horizontal(classes="row"):
            yield Checkbox("Needs review only", id="problems-only")
            yield Button("Export logs...", id="export")
            yield Button("Contents per task", id="contents")
        yield TickTable(COLUMNS, id="logs")
        yield Line(id="ticked", muted=True)
        with Horizontal(classes="row"):
            yield Button("Mark ticked crosschecked", id="mark", variant="success")
            yield Button("Un-mark ticked", id="unmark")
        yield Line(id="title")
        yield Line(id="fit")
        with Horizontal(classes="row"):
            yield Static("Corrected ID:", classes="label")
            yield Input(id="log-id")
            yield Static("Corrected date:", classes="label")
            yield Input(placeholder="YYYY-MM-DD", id="log-date", max_length=10)
            yield Button("Use", id="use")
        yield Line(id="problem")
        with Horizontal(classes="row"):
            yield Input(
                placeholder="Note (optional), e.g. why it's an error", id="note", classes="wide"
            )
        with Horizontal(classes="row"):
            yield Button("Mark OK", id="ok", variant="success")
            yield Button("Mark error", id="error", variant="error")
            yield Button("Use this one", id="use-this")
            yield Button("Clear verdict", id="clear")
        yield Line(id="events-summary")
        yield Table(["onset (s)", "duration (s)", "trial_type", "modulator"], id="events")

    def on_folder_changed(self, key: str) -> None:
        super().on_folder_changed(key)
        if key in ("bids", "behav", "dicom"):
            self.shared.work, self.shared.error = None, ""
            self.shared.load(self)  # in the background: ready by the time you open the tab
            self.shared.changed.emit()

    def on_shared_changed(self) -> None:
        super().on_shared_changed()
        waiting = self.work.waiting() if self.work else 0
        self.toolbox.set_badge(BEHAV, f"{waiting} to review" if waiting else "")

    # ---- the logs -----------------------------------------------------------------------------

    @on(Checkbox.Changed, "#problems-only")
    def fill(self) -> None:
        only = self.query_one("#problems-only", Checkbox).value
        rows = []
        for m in self.work.matches if self.work else []:
            if only and not needs_review(m):
                continue
            info, cells = m.info, log_cells(m)
            review = info.correction.review or ""
            color = VERDICT_COLORS[review] if review else CROSSCHECKED_COLOR
            rows.append(
                (
                    info.key,
                    (
                        cells[0],
                        cells[1],
                        cells[2],
                        cells[3],
                        info.correction.subject or "",
                        date_text(info.correction.date),
                        cell(cells[VERDICT], color),
                        cell(cells[MATCH], OK_COLOR if m.status == MATCHED else ERROR_COLOR),
                        cells[SESSION],
                        cell(cells[STATE], "" if info.state == COMPLETE else WARNING_COLOR),
                        cells[NOTES],
                    ),
                )
            )
        self.query_one("#logs", TickTable).fill(rows)
        self.query_one("#export", Button).disabled = self.work is None
        self.query_one("#contents", Button).disabled = self.work is None
        self.show_log()
        self.update_ticked()

    def current(self) -> LogMatch | None:
        key = self.query_one("#logs", TickTable).current_key()
        matches = self.work.matches if self.work else []
        return next((m for m in matches if m.info.key == key), None)

    @on(Table.RowHighlighted, "#logs")
    def show_log(self) -> None:
        m, work = self.current(), self.work
        for widget_id in ("log-id", "log-date", "use", "note", "ok", "error", "use-this", "clear"):
            self.query_one(f"#{widget_id}").disabled = m is None
        for widget_id in ("title", "fit", "problem", "events-summary"):
            self.query_one(f"#{widget_id}", Line).set("")
        events_table = self.query_one("#events", Table)
        events_table.fill([])
        if m is None or work is None:
            return
        info = m.info
        self.query_one("#log-id", Input).value = info.correction.subject or ""
        self.query_one("#log-id", Input).placeholder = info.file_subject or "ID"
        self.query_one("#log-date", Input).value = date_text(info.correction.date)
        self.query_one("#note", Input).value = info.correction.note
        self.query_one("#use-this", Button).disabled = not same_day_rivals(work.matches, m)
        self.query_one("#title", Line).set(f"{info.key} -- {log_title(m)}")
        events = work.events_of(info)
        if not isinstance(events, EventTable):
            self.query_one("#fit", Line).set(events, ERROR_COLOR)
            return
        self.query_one("#fit", Line).set(*work.fit_of(m, events))
        self.query_one("#events-summary", Line).set("\n".join(events_summary(events)))
        columns = ["onset (s)", "duration (s)", "trial_type", events.modulator]
        if events_table.names != columns:
            events_table.set_columns(columns)
        rows = event_rows(events)[:MAX_EVENT_ROWS]
        events_table.fill([(str(i), row) for i, row in enumerate(rows)])

    # ---- corrections and verdicts -------------------------------------------------------------

    def typed(self) -> dict[str, tuple[str, dt.date | None]]:
        """Every log's correction so far."""
        infos = self.work.infos if self.work else []
        return {i.key: (i.correction.subject or "", i.correction.date) for i in infos}

    def recheck(self, typed: dict[str, tuple[str, dt.date | None]] | None = None) -> None:
        if self.work is not None:
            self.work.set_corrections(typed if typed is not None else self.typed())
            self.shared.recheck()

    @on(Input.Submitted, "#log-id")
    @on(Input.Submitted, "#log-date")
    @on(Button.Pressed, "#use")
    def use_typed(self) -> None:
        m = self.current()
        if m is None:
            return
        try:
            day = parse_date(self.query_one("#log-date", Input).value)
        except ValueError as e:
            self.query_one("#problem", Line).set(str(e), ERROR_COLOR)
            return
        self.recheck(self.typed() | {m.info.key: (self.query_one("#log-id", Input).value, day)})

    def note(self) -> str:
        return self.query_one("#note", Input).value.strip()

    @on(Button.Pressed)
    def on_verdict(self, event: Button.Pressed) -> None:
        m, work = self.current(), self.work
        if work is None:
            return
        match event.button.id:
            case "ok" | "error" if m is not None:
                review = REVIEW_OK if event.button.id == "ok" else REVIEW_ERROR
                work.set_verdict(m.info.key, review, self.note())
            case "clear" if m is not None:
                work.set_verdict(m.info.key, None)
            case "use-this" if m is not None:
                work.use_this_one(m, self.note())
            case "mark":
                work.crosschecked |= self.query_one("#logs", TickTable).ticked
            case "unmark":
                work.crosschecked -= self.query_one("#logs", TickTable).ticked
            case _:
                return
        self.recheck()

    @on(TickTable.Ticked)
    def update_ticked(self) -> None:
        ticked = self.query_one("#logs", TickTable).ticked
        marked = self.work.crosschecked if self.work else set()
        self.query_one("#ticked", Line).set(f"{len(ticked)} ticked (space)")
        self.query_one("#mark", Button).disabled = not ticked - marked
        self.query_one("#unmark", Button).disabled = not ticked & marked

    @on(Button.Pressed, "#contents")
    def contents(self) -> None:
        if self.work is not None:
            self.show_text("Contents per task", inventory_text(self.work.infos) or "No logs.")

    # ---- export -------------------------------------------------------------------------------

    @on(Button.Pressed, "#export")
    def export(self) -> None:
        work = self.work
        if work is None:
            return
        destinations = work.export_destinations()
        texts = list(destinations)

        def picked(index: int | None) -> None:
            if index is None:
                return
            destination = destinations[texts[index]]
            if destination == INTO_BIDS:
                self.ask(
                    "Export into the BIDS folder",
                    EXPORT_INTO_BIDS_QUESTION,
                    lambda: self.run_export(work.bids_root, BIDS_LAYOUT, True),
                )
            else:
                self.pick_layout(destination)

        self.app.push_screen(PickScreen("Put the events files in:", texts), picked)

    def pick_layout(self, destination: str) -> None:
        work = self.work
        layouts = list(EXPORT_LAYOUTS)

        def picked(index: int | None) -> None:
            if index is None or work is None:
                return
            layout = EXPORT_LAYOUTS[layouts[index]]
            if destination == INTO_OUTPUT and work.output_root is not None:
                self.run_export(work.output_root / EXPORT_SUBFOLDER, layout, False)
                return
            self.app.push_screen(
                BrowseScreen("Export the events files to", work.bids_root.parent),
                lambda folder: self.run_export(folder, layout, False) if folder else None,
            )

        self.app.push_screen(PickScreen("Lay the events files out as:", layouts), picked)

    def run_export(self, out_root, layout: str, into_bids: bool) -> None:  # noqa: ANN001 -- Path
        work = self.work
        if work is None:
            return
        if not into_bids and (problem := work.export_problem(out_root)):
            self.show_text("Pick another folder", problem)
            return
        status = self.query_one("#status", Line)
        status.set("Exporting logs...")

        def done(report) -> None:  # noqa: ANN001 -- ExportReport
            self.on_shared_changed()
            self.show_text("Logs exported", export_text(report, out_root, into_bids))

        self.start_job(lambda: work.export(out_root, layout, into_bids), done)
