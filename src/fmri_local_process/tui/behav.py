"""Raw DICOMs and Behaviour logs: two tabs on one read of the logs and DICOM folders."""

import datetime as dt
from collections.abc import Callable

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Input, ProgressBar, Select, Static

from fmri_local_process.app.behav import (
    ALL_LOGS,
    COLUMNS,
    EXPORT_INTO_BIDS_QUESTION,
    EXPORT_LAYOUTS,
    EXPORT_SUBFOLDER,
    HELP_TEXT,
    INTO_BIDS,
    INTO_OUTPUT,
    LOG_VIEWS,
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
    log_shown,
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
    same_day_rivals,
)
from fmri_local_process.tui.widgets import (
    BrowseScreen,
    FilledSelect,
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


def put[T](values: dict[str, T], key: str, value: T | None) -> None:
    """Set it, or take it out when there is none."""
    if value:
        values[key] = value
    else:
        values.pop(key, None)


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
        # The tabs' boxes: what is typed for each row and not used yet, put into the work by
        # these (each gives the first problem found, '' if none).
        self.typed: list[dict] = []
        self.use_typed: list[Callable[[], str]] = []

    def anything_unsaved(self) -> bool:
        return self.unsaved or any(self.typed)

    def take_typed(self) -> str:
        """Everything typed into the work; why not, '' when done."""
        return next(filter(None, (use() for use in self.use_typed)), "")

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
        for typed in self.typed:
            typed.clear()
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
        self.drafts: dict[str, tuple[str, ...]] = {}  # row -> its boxes, as typed
        self.shown: str | None = None  # the row whose values the boxes show

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
        self.shared.typed.append(self.drafts)
        self.shared.use_typed.append(self.use_drafts)
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
            self.show_status()
        self.fill()

    def show_status(self) -> None:
        if self.work is not None:
            unsaved = "   (changes not saved yet)" if self.shared.anything_unsaved() else ""
            text = self.work.summary() + unsaved
            self.query_one("#status", Line).set(text, WARNING_COLOR if unsaved else "")

    def fill(self) -> None: ...

    # ---- what is typed ------------------------------------------------------------------------

    BOXES: tuple[str, ...] = ()  # the Inputs holding one row's values

    def use_drafts(self) -> str:
        """What was typed for every row into the work; the first problem, '' if none."""
        raise NotImplementedError

    def saved_values(self, key: str) -> tuple[str, ...]:
        """One row's values so far, as its boxes show them."""
        raise NotImplementedError

    def show_boxes(self, key: str | None) -> None:
        """The row's values in the boxes: as typed, if something was typed for it."""
        self.shown = key
        values = self.drafts.get(key) or self.saved_values(key) if key else ("",) * len(self.BOXES)
        with self.prevent(Input.Changed):
            for box, value in zip(self.BOXES, values, strict=True):
                self.query_one(box, Input).value = value

    @on(Input.Changed)
    def keep_draft(self, event: Input.Changed) -> None:
        """Typed values stay with their row until they are used, also when another is looked
        at in between."""
        if self.shown is None or f"#{event.input.id}" not in self.BOXES:
            return
        typed = tuple(self.query_one(box, Input).value for box in self.BOXES)
        if typed == self.saved_values(self.shown):
            self.drafts.pop(self.shown, None)
        else:
            self.drafts[self.shown] = typed
        self.show_status()

    def recheck(self) -> bool:
        """Use what is typed on both tabs and match again; False (and why is shown) when some is
        wrong."""
        if problem := self.shared.take_typed():
            self.query_one("#problem", Line).set(problem, ERROR_COLOR)
            return False
        self.shared.recheck()
        return True

    @on(Button.Pressed, "#reload")
    def reload(self) -> None:
        if self.shared.anything_unsaved():
            question = "Corrections not saved yet are dropped. Read everything again?"
            self.ask("Reload", question, lambda: self.shared.load(self))
        else:
            self.shared.load(self)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        if problem := self.shared.take_typed():
            self.show_text("Not saved", problem)
            return
        if self.work is not None:
            self.work.match()
        try:
            self.shared.save()
        except OSError as e:
            self.show_text("Not saved", str(e))
            return
        self.show_text("Saved", "Corrections saved. Convert to BIDS (tab 1) to apply them.")


class SessionsPage(SharedPage):
    """Raw DICOMs: every DICOM session folder, with its corrections."""

    GUIDE = SESSIONS_HELP_TEXT
    BOXES = ("#folder-id", "#folder-date", "#folder-session")

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
            yield Button("Re-check", id="use")
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
        self.show_boxes(name if work is not None else None)
        if name is None or work is None:
            self.query_one("#picked", Line).set("")
            return
        row = [f.name for f, _tasks in work.scanned].index(name)
        self.query_one("#picked", Line).set(f"{name}   {work.folder_cells(row)[S_NOTES]}")

    def saved_values(self, key: str) -> tuple[str, ...]:
        if self.work is None:
            return "", "", ""
        fixes = self.work.fixes
        return fixes.ids.get(key, ""), date_text(fixes.dates.get(key)), fixes.sessions.get(key, "")

    def use_drafts(self) -> str:
        work = self.work
        if work is None:
            return ""
        fixes = work.fixes
        for name, (subject, day_text, session_text) in list(self.drafts.items()):
            try:
                day, session = parse_date(day_text), parse_session(session_text)
            except ValueError as e:
                return f"{name}: {e}"
            put(fixes.ids, name, subject.strip())
            put(fixes.dates, name, day)
            put(fixes.sessions, name, session)
            del self.drafts[name]
            self.shared.unsaved = True
        return ""

    @on(Input.Submitted)
    @on(Button.Pressed, "#use")
    def use_typed(self) -> None:
        """The corrections as typed: matched again, not saved yet."""
        self.recheck()

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
        self.recheck()


class BehavPage(SharedPage):
    """Behaviour logs: match each log to a session, look at its events, give a verdict."""

    GUIDE = HELP_TEXT
    BOXES = ("#log-id", "#log-date", "#note")

    def compose(self) -> ComposeResult:
        yield from self.compose_controls()
        with Horizontal(classes="row"):
            yield FilledSelect([(v, v) for v in LOG_VIEWS], id="log-view", allow_blank=False)
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
            yield Button("Re-check", id="use")
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

    @on(Select.Changed, "#log-view")
    def fill(self) -> None:
        view = self.query_one("#log-view", FilledSelect).chosen() or ALL_LOGS
        rows = []
        for m in self.work.matches if self.work else []:
            if not log_shown(m, view):
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
        self.show_boxes(m.info.key if m is not None else None)
        if m is None or work is None:
            return
        info = m.info
        self.query_one("#log-id", Input).placeholder = info.file_subject or "ID"
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

    def saved_values(self, key: str) -> tuple[str, ...]:
        info = next((i for i in self.shared.infos() if i.key == key), None)
        if info is None or self.work is None:
            return "", "", ""
        note = self.work.verdicts.get(key, ("", ""))[1]
        return info.correction.subject or "", date_text(info.correction.date), note

    def use_drafts(self) -> str:
        """A note goes with a verdict: typed for a log without one, it stays as typed."""
        work = self.work
        if work is None:
            return ""
        typed = {
            i.key: (i.correction.subject or "", i.correction.date) for i in self.shared.infos()
        }
        for key, (subject, day_text, note) in list(self.drafts.items()):
            try:
                typed[key] = (subject, parse_date(day_text))
            except ValueError as e:
                return f"{key}: {e}"
            review, old_note = work.verdicts.get(key, (None, ""))
            if review and note.strip() != old_note:
                work.set_verdict(key, review, note.strip())
        work.set_corrections(typed)
        for key, (_subject, _day, note) in list(self.drafts.items()):
            if self.saved_values(key)[2] == note.strip():
                del self.drafts[key]
            else:  # only its note is left to use
                self.drafts[key] = (*self.saved_values(key)[:2], note)
            self.shared.unsaved = True
        return ""

    @on(Input.Submitted, "#log-id")
    @on(Input.Submitted, "#log-date")
    @on(Input.Submitted, "#note")
    @on(Button.Pressed, "#use")
    def use_typed(self) -> None:
        self.recheck()

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
                self.clear_note(m.info.key)
            case "use-this" if m is not None:
                work.use_this_one(m, self.note())
            case "mark":
                work.crosschecked |= self.query_one("#logs", TickTable).ticked
            case "unmark":
                work.crosschecked -= self.query_one("#logs", TickTable).ticked
            case _:
                return
        self.recheck()

    def clear_note(self, key: str) -> None:
        """Clearing a verdict clears its note too."""
        if key in self.drafts:
            self.drafts[key] = (*self.drafts[key][:2], "")

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
        if work is None or not self.recheck():  # as the table shows it, with what is typed
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
