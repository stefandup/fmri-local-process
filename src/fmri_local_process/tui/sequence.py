"""Sequence overview: a subject's runs of one scan with their scan parameters side by side."""

from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, HorizontalScroll
from textual.widgets import Button, Checkbox, Select, Static

from fmri_local_process.app.notices import bids_notices
from fmri_local_process.app.sequence import ReadJob, overview_text, save_overview, status_text
from fmri_local_process.app.state import ToolboxState
from fmri_local_process.processing import sequence_params as sp
from fmri_local_process.tui.widgets import FilledSelect, Line, Notices, Page


class SequencePage(Page):
    GUIDE = (
        "A subject's runs of one scan (e.g. ses-1 and ses-2) with their scan parameters side by "
        "side. Rows where the runs differ are marked ≠. The values come from the NIfTI header "
        "and the BIDS sidecar; Read DICOM headers fills in what those lack (patient fields are "
        "never read)."
    )

    def __init__(self, state: ToolboxState) -> None:
        super().__init__(state)
        self.seen: tuple = ()
        self.runs: list[sp.ScanRun] = []
        self.dicom_read = False
        self.reading = False

    def compose(self) -> ComposeResult:
        yield Notices()
        with Horizontal(classes="row"):
            yield FilledSelect([], prompt="Subject", id="subject", allow_blank=True)
            yield FilledSelect([], prompt="Scan", id="scan", allow_blank=True)
            yield Checkbox("Only rows that differ", id="differ")
        with Horizontal(classes="row"):
            yield Button("Read DICOM headers", id="dicom")
            yield Button("Refresh", id="refresh")
            yield Button("Save...", id="save")
        yield Line(id="status", muted=True)
        with HorizontalScroll(id="text-scroll", classes="row"):
            yield Static(id="text", markup=False)

    def on_mount(self) -> None:
        self.query_one("#text", Static).styles.width = "auto"

    def show_page(self) -> None:
        if self.seen != (self.state.generation, self.state.folder("bids")):
            self.refresh_subjects()

    @on(Button.Pressed, "#refresh")
    def refresh_subjects(self) -> None:
        bids = self.state.folder("bids")
        self.seen = (self.state.generation, bids)
        names = sp.subjects(bids) if bids is not None and bids.is_dir() else []
        self.query_one("#subject", FilledSelect).fill([(n, n) for n in names])
        self.query_one(Notices).set_messages(bids_notices(bids, bool(names)))
        self.load_subject()

    @on(Select.Changed, "#subject")
    def load_subject(self) -> None:
        bids, subject = self.state.folder("bids"), self.subject()
        if bids is None or not subject:
            self.runs = []
            self.show_table()
        elif not self.reading:
            self.read(sp.find_runs(bids, subject), dicom=False)

    def subject(self) -> str:
        return self.query_one("#subject", FilledSelect).chosen() or ""

    def scan(self) -> str:
        return self.query_one("#scan", FilledSelect).chosen() or ""

    @on(Button.Pressed, "#dicom")
    def read_dicom(self) -> None:
        if self.state.folder("dicom") is None:
            self.show_text("No DICOM folder", "Choose the DICOM folder on the Setup & Convert tab.")
            return
        self.read(self.runs, dicom=True)

    def read(self, runs: list[sp.ScanRun], dicom: bool) -> None:
        bids = self.state.folder("bids")
        if bids is None or self.reading:
            return
        self.reading, self.dicom_read = True, dicom
        self.query_one("#status", Line).set(
            "Reading DICOM headers..." if dicom else "Reading headers..."
        )
        job = ReadJob(runs, bids, self.state.folder("dicom") if dicom else None)
        self.update_buttons()
        self.start_job(
            job.run,
            lambda error: self.on_read(runs, error),
            lambda error: self.on_read(runs, error),
        )

    def on_read(self, runs: list[sp.ScanRun], error: str) -> None:
        self.reading = False
        self.runs = runs
        if runs and runs[0].subject != self.subject():
            self.load_subject()  # the subject was changed while reading
            return
        if error:
            self.show_text("Not all headers read", error)
        self.query_one("#scan", FilledSelect).fill([(s, s) for s in sp.scans(runs)])
        self.show_table()

    def shown_runs(self) -> list[sp.ScanRun]:
        return [r for r in self.runs if r.scan == self.scan()]

    def text(self) -> str:
        differ = self.query_one("#differ", Checkbox).value
        return overview_text(self.shown_runs(), self.subject(), self.scan(), differ)

    @on(Select.Changed, "#scan")
    @on(Checkbox.Changed, "#differ")
    def show_table(self) -> None:
        self.query_one("#text", Static).update(self.text())
        if not self.reading:
            self.query_one("#status", Line).set(
                status_text(self.dicom_read, self.state.folder("dicom"))
            )
        self.update_buttons()

    def update_buttons(self) -> None:
        self.query_one("#dicom", Button).disabled = self.reading or not self.runs
        self.query_one("#save", Button).disabled = self.reading or not self.shown_runs()

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        runs, bids = self.shown_runs(), self.state.folder("bids")
        if not runs or bids is None:
            return
        start = bids.parent / f"sub-{runs[0].subject}_{self.scan()}_sequence.txt"

        def write(path: Path) -> None:
            try:
                save_overview(runs, self.text(), path)
            except OSError as e:
                self.show_text("Not saved", str(e))
                return
            self.query_one("#status", Line).set(f"Saved {path}")

        self.ask_save_path("Save as text (.txt) or CSV (.csv):", start, write)
