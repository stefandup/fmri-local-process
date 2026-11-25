"""Scan lengths & trimming: the expected volumes per scan, trimming, and the DICOM volume check."""

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Checkbox, Input, Static

from fmri_local_process.app import dicom_volumes as adv
from fmri_local_process.app.scan_lengths import (
    COLUMNS,
    HELP_TEXT,
    LONG_COLOR,
    MAX_MARGIN_S,
    MAX_VOLUMES,
    TRIMMED_COLOR,
    ScanLengths,
    length_note,
    no_scans_notices,
    trimmed_note,
)
from fmri_local_process.app.state import ToolboxState
from fmri_local_process.processing import dicom_volumes as dv
from fmri_local_process.processing.crosscheck import (
    DataFile,
    find_data_files,
    log_event_ends,
    long_runs,
    review_bids_folder,
)
from fmri_local_process.tui.trimming import start_trimming
from fmri_local_process.tui.widgets import (
    Activity,
    Line,
    Notices,
    Page,
    Table,
    cell,
    decimal,
    plain,
    whole_number,
)


class ScanLengthsPage(Page):
    def __init__(self, state: ToolboxState) -> None:
        super().__init__(state)
        self.seen: tuple = ()
        self.lengths: ScanLengths | None = None
        self.data_files: list[DataFile] = []
        self.checks: list[dv.VolumeCheck] = []
        self.check_job: adv.CheckJob | None = None

    def help_text(self) -> str:
        saved = f"\n\n{self.lengths.saved_text()}" if self.lengths else ""
        return f"{plain(HELP_TEXT)}{saved}\n\nMissing volumes\n{plain(adv.HELP_TEXT)}"

    def compose(self) -> ComposeResult:
        yield Notices()
        with Horizontal(classes="row"):
            yield Button("Trim long runs...", id="trim", variant="success")
        yield Table(COLUMNS, id="lengths")
        yield Line(id="picked")
        with Horizontal(classes="row"):
            yield Static("Expected volumes:", classes="label")
            yield Input(placeholder="automatic", id="expected")
            yield Static("Margin (s):", classes="label")
            yield Input(id="margin")
            yield Button("Save", id="save")
            yield Button("Use the automatic length", id="automatic")
        yield Line(id="saved", muted=True)
        yield Activity()
        yield Static("Missing volumes: DICOM images vs NIfTI", classes="section")
        with Horizontal(classes="row"):
            yield Button("Check DICOMs for missing volumes", id="check")
            yield Checkbox("Only runs with problems", id="problems-only")
        yield Line(id="check-status", muted=True)
        yield Table([name for name, _tip in adv.COLUMNS], id="checks")
        yield Line(id="explanation")

    def on_mount(self) -> None:
        self.state.busy_changed.connect(self.update_controls)
        self.toolbox.behav_shared.changed.connect(self.refresh_if_shown)

    @property
    def activity(self) -> Activity:
        return self.query_one(Activity)

    def refresh_if_shown(self) -> None:
        self.seen = ()  # the logs were (re)read: suggest lengths from them
        if self.toolbox.current_page() is self:
            self.show_page()

    def show_page(self) -> None:
        seen = (self.state.generation, self.state.folder("bids"), self.state.folder("dicom"))
        if self.seen != seen:
            self.seen = seen
            self.refresh_lengths()

    def refresh_lengths(self) -> None:
        bids = self.state.folder("bids")
        files = find_data_files(bids) if bids is not None else []
        if files != self.data_files:  # new contents: the old check no longer holds
            self.checks = []
        self.data_files = files
        ends = log_event_ends(self.toolbox.behav_shared.infos(), files)
        self.lengths = ScanLengths(bids, files, ends) if bids is not None else None
        has_scans = self.lengths is not None and bool(self.lengths.scans)
        self.query_one(Notices).set_messages(no_scans_notices(bids, has_scans))
        self.fill_lengths()
        self.fill_checks()
        self.update_controls()

    def fill_lengths(self) -> None:
        rows = []
        lengths = self.lengths
        for label, runs in (lengths.scans if lengths else {}).items():
            assert lengths is not None
            chosen = lengths.set[label]
            too_long = len(lengths.too_long(label))
            cells = (
                label,
                len(runs),
                length_note(runs),
                chosen or lengths.automatic_text(label),
                lengths.from_logs(label)[0],
                f"{lengths.margins[label]:g}" if label in lengths.margins else "",
                lengths.minutes(label),
                cell(too_long or "", LONG_COLOR),
                cell(trimmed_note(runs), TRIMMED_COLOR),
            )
            rows.append((label, cells))
        self.query_one("#lengths", Table).fill(rows)
        self.show_scan()

    def label(self) -> str | None:
        return self.query_one("#lengths", Table).current_key()

    @on(Table.RowHighlighted, "#lengths")
    def show_scan(self) -> None:
        label, lengths = self.label(), self.lengths
        known = lengths is not None and label in lengths.scans
        for widget_id in ("#expected", "#margin", "#save", "#automatic"):
            self.query_one(widget_id).disabled = not known
        if lengths is None or label is None or not known:
            self.query_one("#picked", Line).set("")
            return
        expected, margin = self.query_one("#expected", Input), self.query_one("#margin", Input)
        expected.value = str(lengths.set[label] or "")
        expected.placeholder = lengths.automatic_text(label)
        margin.disabled = label not in lengths.margins
        margin.value = f"{lengths.margins[label]:g}" if label in lengths.margins else ""
        details = [f"{label}: {lengths.from_logs(label)[1]}"]
        if long_text := lengths.too_long_details(label):
            details.append(f"Too long:\n{long_text}")
        self.query_one("#picked", Line).set("\n".join(details))

    @on(Input.Submitted, "#expected")
    @on(Input.Submitted, "#margin")
    @on(Button.Pressed, "#save")
    def save(self) -> None:
        label, lengths = self.label(), self.lengths
        if lengths is None or label is None:
            return
        typed = self.query_one("#expected", Input).value.strip()
        volumes = whole_number(typed, 0, MAX_VOLUMES) if typed else 0
        if volumes is None:
            self.show_text("Not saved", "Expected volumes: give a whole number, or leave it empty.")
            return
        if label in lengths.margins:
            margin = decimal(self.query_one("#margin", Input).value, 0, MAX_MARGIN_S)
            if margin is None:
                self.show_text("Not saved", f"Margin: give seconds from 0 to {MAX_MARGIN_S:g}.")
                return
            lengths.margins[label] = margin
        lengths.set[label] = volumes
        self.save_lengths(lengths)

    @on(Button.Pressed, "#automatic")
    def use_automatic(self) -> None:
        label = self.label()
        if self.lengths is not None and label is not None:
            self.lengths.set[label] = 0
            self.save_lengths(self.lengths)

    def save_lengths(self, lengths: ScanLengths) -> None:
        try:
            lengths.save()
        except OSError as e:
            self.show_text("Not saved", str(e))
            return
        self.query_one("#saved", Line).set("Lengths saved")
        self.state.bump()  # the crosscheck's short / long flags follow

    # ---- trimming -----------------------------------------------------------------------------

    def update_controls(self) -> None:
        count = self.lengths.long_count() if self.lengths else 0
        reason = self.state.lock_reason()
        trim = self.query_one("#trim", Button)
        trim.label = f"Trim {count} long run(s)..."
        trim.disabled = count == 0 or bool(reason)
        dicom = self.state.folder("dicom")
        runs = dv.bold_runs(self.data_files)
        self.query_one("#check", Button).disabled = (
            self.check_job is not None or dicom is None or not runs
        )
        if self.check_job is None:
            text = adv.status_text(dicom, runs, self.checks)
            self.query_one("#check-status", Line).set(text)

    @on(Button.Pressed, "#trim")
    def trim(self) -> None:
        bids = self.state.folder("bids")
        if bids is not None:
            start_trimming(self, long_runs(review_bids_folder(bids)), self.activity.say)

    # ---- missing volumes ----------------------------------------------------------------------

    @on(Button.Pressed, "#check")
    def check(self) -> None:
        bids, dicom = self.state.folder("bids"), self.state.folder("dicom")
        if bids is None or dicom is None or self.check_job is not None:
            return
        status = self.query_one("#check-status", Line)
        self.check_job = adv.CheckJob(
            bids,
            dicom,
            dv.bold_runs(self.data_files),
            lambda done, total: self.tell(status.set, f"Reading DICOMs: run {done} of {total}..."),
        )
        status.set("Reading DICOMs...")
        self.update_controls()
        self.start_job(
            self.check_job.run,
            lambda result: self.on_checked(*result),
            lambda error: self.on_checked([], error),
        )

    def on_checked(self, checks: list[dv.VolumeCheck], error: str) -> None:
        self.check_job = None
        self.checks = checks
        self.fill_checks()
        self.update_controls()
        if error:
            self.query_one("#check-status", Line).set(f"DICOMs not read: {error}")

    @on(Checkbox.Changed, "#problems-only")
    def fill_checks(self) -> None:
        only = self.query_one("#problems-only", Checkbox).value
        rows = []
        for i, check in enumerate(self.checks):
            if only and check.result not in adv.PROBLEMS:
                continue
            cells = [cell(text) for text in adv.check_cells(check)]
            cells[-1] = cell(check.result, adv.RESULT_COLOR[check.result])
            rows.append((str(i), cells))
        self.query_one("#checks", Table).fill(rows)
        self.explain()

    @on(Table.RowHighlighted, "#checks")
    def explain(self) -> None:
        key = self.query_one("#checks", Table).current_key()
        check = self.checks[int(key)] if key is not None else None
        text = f"{check.name}: {check.explanation}" if check else ""
        self.query_one("#explanation", Line).set(text)
