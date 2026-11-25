"""First-level stats: fit the task GLM to each run of a subject and compare the runs."""

from pathlib import Path

from matplotlib.figure import Figure
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Checkbox, Input, Select, Static

from fmri_local_process.app import figures
from fmri_local_process.app.first_level import (
    DIFFERS_COLOR,
    NO_ATLAS,
    SHOW_CURSOR,
    SHOW_PEAK,
    SHOW_REGION,
    FirstLevelModel,
    ModelJob,
    differs,
    fetch_atlases,
    modelled_text,
    subject_notices,
)
from fmri_local_process.app.metric_help import help_lines
from fmri_local_process.app.state import ToolboxState
from fmri_local_process.processing import atlases as at
from fmri_local_process.processing import first_level as fl
from fmri_local_process.processing.metric_docs import METRIC_DOCS
from fmri_local_process.tui.images import ImagePanel
from fmri_local_process.tui.widgets import (
    FilledSelect,
    Line,
    Notices,
    Page,
    Table,
    TickTable,
    cell,
    decimal,
    on_background,
)

BRAIN_SIZE = (10.0, 3.0)  # inches; the height is per run
TIME_SIZE = (12.0, 2.4)
NONE = "none"  # the atlas dropdown's value for 'no atlas'


class FirstLevelPage(Page):
    GUIDE = (
        "Does the task give the expected brain response? Pick a subject and Run: the block design "
        "is fitted to each of the subject's runs of the task, and the runs are compared.\n\n"
        "Regions come from a standard atlas, copied out of the C-PAC image once; tick them with "
        "space. With no atlas, the whole brain is the region.\n\n"
        "Each run has its own cursor, starting at its own peak. Type an MNI position to move the "
        "highlighted run's cursor; Lock cursors moves them all together."
    )

    def __init__(self, state: ToolboxState) -> None:
        super().__init__(state)
        self.model = FirstLevelModel()
        self.seen_output: Path | None = None
        self.modelling = False
        self.ready = False

    def compose(self) -> ComposeResult:
        design = self.model.setup.design
        yield Notices()
        with Horizontal(classes="row"):
            yield FilledSelect([(s.name, s.key) for s in fl.SETUPS], id="setup", allow_blank=False)
            yield FilledSelect([], prompt="Subject", id="subject")
            yield FilledSelect(
                [(text, reg) for reg, text in fl.DATA_CHOICES.items()], id="data", allow_blank=False
            )
        with Horizontal(classes="row"):
            yield Static("t >", classes="label")
            yield Input(f"{fl.DEFAULT_T_THRESHOLD:g}", id="threshold")
            yield Button("Run first-level stats", id="run", variant="success")
        yield Line(id="status", muted=True)
        with Horizontal(classes="row"):
            yield Static("Design: first block", classes="label")
            yield FilledSelect(
                [("off", "off"), ("on", "on")], value=design.first_block, id="first",
                allow_blank=False,
            )  # fmt: skip
            for text, key, value in (
                ("ON (s):", "on", design.on_s),
                ("OFF (s):", "off", design.off_s),
                ("Task length (s):", "total", design.total_s),
            ):
                yield Static(text, classes="label")
                yield Input(f"{value:g}", id=f"design-{key}")
        yield Line(self.model.setup.about, id="about", muted=True)
        yield Static("Regions", classes="section")
        with Horizontal(classes="row"):
            yield FilledSelect(
                [(NO_ATLAS, NONE), *((spec.name, spec.key) for spec in at.ATLASES)],
                id="atlas",
                allow_blank=False,
            )
            yield Button("Get atlases from the C-PAC image", id="fetch")
            yield Button("Focus", id="focus-regions")
            yield Button("All", id="all-regions")
            yield Button("None", id="no-regions")
        yield Input(placeholder="Filter regions...", id="filter")
        yield TickTable(["Region"], id="regions")
        yield Line(id="region-count", muted=True)
        yield Static("Brain", classes="section")
        with Horizontal(classes="row"):
            yield Button("Cursor to peak", id="peak")
            yield Checkbox("Lock cursors", id="lock")
            yield FilledSelect([], prompt="Run", id="cursor-run")
            yield Input(placeholder="MNI x, y, z (Enter)", id="mni")
        yield Line(id="cursor-text", muted=True)
        yield ImagePanel("Run first-level stats to see the maps.", id="brain")
        yield Static("Time courses", classes="section")
        with Horizontal(classes="row"):
            yield FilledSelect(
                [(text, text) for text in (SHOW_PEAK, SHOW_CURSOR, SHOW_REGION)],
                id="show",
                allow_blank=False,
            )
        yield ImagePanel("", id="times")
        yield Static("Comparison", classes="section")
        with Horizontal(classes="row"):
            yield Button("Metric formulas", id="formulas")
            yield Button("Save maps (NIfTI)", id="save-maps")
            yield Button("Export CSV...", id="export")
        yield Table([], id="comparison")
        yield Line(id="metric-help", muted=True)

    def on_mount(self) -> None:
        self.ready = True
        self.use_setup(fl.SETUPS[0])
        self.call_after_refresh(self.auto_run)

    def show_page(self) -> None:
        if self.ready and self.state.folder("output") != self.seen_output:
            self.fill_subjects()
            self.auto_run()
        self.query_one("#fetch").display = not at.atlases_present(self.model.cache)

    # ---- setup and subjects -------------------------------------------------------------------

    @on(Select.Changed, "#setup")
    def on_setup(self) -> None:
        key = self.query_one("#setup", FilledSelect).chosen()
        setup = next((s for s in fl.SETUPS if s.key == key), None)
        if self.ready and setup is not None and setup is not self.model.setup:
            self.use_setup(setup)

    def use_setup(self, setup: fl.FirstLevelSetup) -> None:
        atlas, design = self.model.use_setup(setup), setup.design
        self.query_one("#first", FilledSelect).value = design.first_block
        for key, value in (("on", design.on_s), ("off", design.off_s), ("total", design.total_s)):
            self.query_one(f"#design-{key}", Input).value = f"{value:g}"
        self.query_one("#about", Line).set(setup.about)
        select = self.query_one("#atlas", FilledSelect)
        with select.prevent(Select.Changed):
            select.value = atlas or NONE
        self.use_atlas()
        self.fill_subjects()

    def design(self) -> fl.BlockDesign | None:
        seconds = [
            decimal(self.query_one(f"#design-{key}", Input).value, 0.5, 3600.0)
            for key in ("on", "off", "total")
        ]
        on_s, off_s, total_s = seconds
        first = self.query_one("#first", FilledSelect).chosen()
        if on_s is None or off_s is None or total_s is None or first is None:
            return None
        return fl.BlockDesign(first_block=first, on_s=on_s, off_s=off_s, total_s=total_s)

    def fill_subjects(self) -> None:
        output, task = self.state.folder("output"), self.model.setup.task
        self.seen_output = output
        subjects = fl.output_subjects(output, task) if output else []
        self.query_one("#subject", FilledSelect).fill([(f"sub-{s}", s) for s in subjects])
        self.query_one(Notices).set_messages(subject_notices(output, subjects, task))
        self.query_one("#run", Button).disabled = not subjects
        text = f"{len(subjects)} subject(s) with task-{task}." if subjects else ""
        self.query_one("#status", Line).set(text)

    # ---- regions ------------------------------------------------------------------------------

    @on(Select.Changed, "#atlas")
    def use_atlas(self) -> None:
        if not self.ready:
            return
        chosen = self.query_one("#atlas", FilledSelect).chosen() or NONE
        problem = self.model.use_atlas("" if chosen == NONE else chosen)
        if problem:
            self.query_one("#status", Line).set(problem)
        self.fill_regions()
        self.update_stats()

    @on(Input.Changed, "#filter")
    def fill_regions(self) -> None:
        words = self.query_one("#filter", Input).value.lower().split()
        table = self.query_one("#regions", TickTable)
        shown = [r for r in self.model.regions() if all(w in r.name.lower() for w in words)]
        table.ticked = {str(v) for v in self.model.ticked_values()}
        table.fill([(str(r.value), (r.name,)) for r in shown])
        self.query_one("#region-count", Line).set(self.model.region_text())

    @on(TickTable.Ticked)
    def on_ticked(self, event: TickTable.Ticked) -> None:
        shown = {int(key) for key in event.table.row_keys}
        ticked = {int(key) for key in event.table.ticked}
        self.tick((self.model.ticked_values() - shown) | ticked)  # filtered-out ones stay

    def tick(self, values: set[int]) -> None:
        self.model.tick(values)
        self.fill_regions()
        self.update_stats()

    @on(Button.Pressed, "#fetch")
    def fetch(self) -> None:
        self.query_one("#fetch", Button).disabled = True
        self.query_one("#status", Line).set("Copying the atlases out of the C-PAC image...")

        def done(error: str) -> None:
            self.query_one("#fetch", Button).disabled = False
            if error:
                self.query_one("#status", Line).set("Atlases not copied.")
                self.show_text("Atlases not copied", error)
                return
            self.query_one("#status", Line).set(f"Atlases copied to {self.model.cache}.")
            self.query_one("#fetch").display = False
            self.use_setup(self.model.setup)

        self.start_job(fetch_atlases, done, done)

    # ---- running ------------------------------------------------------------------------------

    def auto_run(self) -> None:
        """Model the first subject at once, so the tab opens on results."""
        if not self.model.results and not self.modelling:
            self.run_model(quiet=True)

    @on(Button.Pressed, "#run")
    def on_run(self) -> None:
        self.run_model(quiet=False)

    def run_model(self, quiet: bool) -> None:
        output = self.state.folder("output")
        subject = self.query_one("#subject", FilledSelect).chosen()
        reg = self.query_one("#data", FilledSelect).chosen() or next(iter(fl.DATA_CHOICES))
        design = self.design()
        if output is None or not subject or self.modelling:
            return
        task = self.model.setup.task
        runs = fl.find_runs(output, subject, task, reg)
        if not runs or design is None:
            if not quiet:
                text = (
                    f"No reg-{reg} MNI-space task-{task} BOLD."
                    if not runs
                    else "Design: give seconds from 0.5 to 3600."
                )
                self.show_text("Nothing to model", text)
            return
        self.modelling = True
        self.query_one("#run", Button).disabled = True
        self.query_one("#status", Line).set(f"Modelling {len(runs)} run(s) of sub-{subject}...")
        job = ModelJob(runs, design, reg, self.state.folder("bids"))
        self.start_job(
            job.run,
            lambda result: self.on_modelled(*result),
            lambda error: self.on_modelled([], error),
        )

    def on_modelled(self, results: list[fl.RunResult], errors: str) -> None:
        self.modelling = False
        self.query_one("#run", Button).disabled = False
        self.model.set_results(results)
        if errors:
            self.show_text("Some runs not modelled", errors)
        self.query_one("#status", Line).set(modelled_text(results))
        runs = [(r.run.label, str(i)) for i, r in enumerate(results)]
        self.query_one("#cursor-run", FilledSelect).fill(runs)
        self.update_stats()
        self.to_peak()

    @on(Input.Submitted, "#threshold")
    def update_stats(self) -> None:
        threshold = decimal(self.query_one("#threshold", Input).value, 0.0, 50.0)
        if threshold is not None:
            self.model.threshold = threshold
        if self.model.results:
            self.model.update_stats()
        self.fill_table()
        self.draw()

    # ---- cursors and figures ------------------------------------------------------------------

    @on(Button.Pressed, "#peak")
    def to_peak(self) -> None:
        self.model.cursors_to_peak()
        self.draw()

    @on(Checkbox.Changed, "#lock")
    def on_lock(self, event: Checkbox.Changed) -> None:
        self.model.lock(event.value)
        self.draw()

    @on(Input.Submitted, "#mni")
    def on_mni(self, event: Input.Submitted) -> None:
        values = [decimal(part, -200.0, 200.0) for part in event.value.replace(",", " ").split()]
        row = self.query_one("#cursor-run", FilledSelect).chosen()
        if len(values) != 3 or None in values or row is None:
            self.query_one("#cursor-text", Line).set("Give three numbers, e.g. -8 -92 4", "#e74c3c")
            return
        x, y, z = (v for v in values if v is not None)
        self.model.move_cursor(int(row), (x, y, z))
        show = self.query_one("#show", FilledSelect)
        with show.prevent(Select.Changed):
            show.value = SHOW_CURSOR
        self.draw()

    @on(Select.Changed, "#show")
    def draw(self) -> None:
        if not self.ready or not self.model.results:
            return
        runs = len(self.model.results)
        brain = Figure(figsize=(BRAIN_SIZE[0], BRAIN_SIZE[1] * runs), facecolor="black")
        self.query_one("#cursor-text", Line).set(self.model.draw_brain(brain))
        times = Figure(figsize=(TIME_SIZE[0], TIME_SIZE[1] * runs))
        mode = self.query_one("#show", FilledSelect).chosen() or SHOW_PEAK
        self.model.draw_time_courses(times, mode)
        name = f"sub-{self.model.subject()}_{self.model.setup.key}"
        self.query_one("#brain", ImagePanel).show_png(figures.png_bytes(brain), f"{name}_maps")
        self.query_one("#times", ImagePanel).show_png(
            figures.png_bytes(times), f"{name}_time-courses"
        )

    # ---- the comparison table -----------------------------------------------------------------

    def fill_table(self) -> None:
        rows = self.model.comparison()
        table = self.query_one("#comparison", Table)
        columns = ["Metric", *(r.run.label for r in self.model.results)]
        if table.names != columns:
            table.set_columns(columns)
        table.fill(
            [
                (
                    name,
                    (
                        name,
                        *(
                            on_background(v, DIFFERS_COLOR) if differs(name, values) else cell(v)
                            for v in values
                        ),
                    ),
                )
                for name, values in rows
            ]
        )
        self.query_one("#export", Button).disabled = not rows
        self.query_one("#save-maps", Button).disabled = not rows

    @on(Table.RowHighlighted, "#comparison")
    def show_metric(self) -> None:
        name = self.query_one("#comparison", Table).current_key() or ""
        self.query_one("#metric-help", Line).set(fl.METRIC_HELP.get(name, ""))

    @on(Button.Pressed)
    def on_button(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "focus-regions":
                self.tick(self.model.focus_regions())
            case "all-regions":
                self.tick({r.value for r in self.model.regions()})
            case "no-regions":
                self.tick(set())
            case "formulas":
                title = "How the first-level metrics are calculated"
                self.show_text(title, "\n".join(help_lines(METRIC_DOCS)))
            case "export":
                self.export()
            case "save-maps":
                self.save_maps()

    def export(self) -> None:
        output = self.state.folder("output")
        if output is None:
            return

        def write(path: Path) -> None:
            try:
                self.model.export_csv(path)
            except OSError as e:
                self.show_text("Not exported", str(e))
                return
            self.show_text("Exported", f"Saved {path}")

        self.ask_save_path("Export CSV to:", output / self.model.csv_name(), write)

    def save_maps(self) -> None:
        output = self.state.folder("output")
        if output is None:
            return

        def done(result: tuple[int, Path]) -> None:
            self.show_text("Saved", f"{result[0]} map(s) saved in {result[1]}")

        def failed(error: str) -> None:
            self.show_text("Maps not saved", error)

        self.start_job(lambda: self.model.save_maps(output), done, failed)
