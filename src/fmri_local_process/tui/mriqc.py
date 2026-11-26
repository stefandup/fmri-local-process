"""Scan quality (MRIQC): run MRIQC, read its metrics, look at a run and rate it."""

from collections import OrderedDict
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Input, Select, Static

from fmri_local_process.app import mriqc as am
from fmri_local_process.app.colors import ERROR_COLOR
from fmri_local_process.app.metric_help import help_lines
from fmri_local_process.app.settings import saved_number
from fmri_local_process.app.state import QUALITY_CHECKING, VIEW_ONLY, ToolboxState
from fmri_local_process.processing import mriqc as mq
from fmri_local_process.processing.mriqc_docs import MRIQC_DOCS
from fmri_local_process.processing.review_mode import packaged_mriqc
from fmri_local_process.tui.images import ImagePanel
from fmri_local_process.tui.widgets import (
    Activity,
    DockerLine,
    FilledSelect,
    FolderBox,
    ImageBox,
    Line,
    Notices,
    Page,
    RunBox,
    Table,
    TickTable,
    cell,
    decimal,
    on_background,
    whole_number,
)

MAX_CPUS, MAX_MEM_GB = 128, 512.0
DOCKER_RECHECK_S = 30
VIEW_CACHE_SIZE = 8  # drawn scans kept: going back to one is instant
PICK_A_RUN = "Highlight a run above to see the scan here and rate it."


class Listener:
    """The job's calls (on its thread), passed on to the page on the UI thread."""

    def __init__(self, page: "MriqcPage") -> None:
        self.page = page

    def on_message(self, text: str, is_error: bool, /) -> None:
        self.page.tell(self.page.activity.say, text, is_error)

    def on_subject_started(self, subject: str, /) -> None:
        self.page.tell(self.page.subject_started, subject)

    def on_step(self, subject: str, steps: int, node: str, /) -> None:
        self.page.tell(self.page.step, subject, steps, node)

    def on_subject_done(self, subject: str, failure: str, /) -> None:
        self.page.tell(self.page.subject_done, subject, failure)

    def on_stage(self, text: str, /) -> None:
        self.page.tell(self.page.run_box.set_stage, text)

    def on_all_done(self, summary: str, /) -> None:
        self.page.tell(self.page.all_done, summary)


class MriqcPage(Page):
    GUIDE = am.HELP_TEXT

    def __init__(self, state: ToolboxState) -> None:
        super().__init__(state)
        self.packaged_results = packaged_mriqc()
        self.seen: tuple = ()
        self.subjects: list[str] = []
        self.statuses: dict[str, str] = {}
        self.notes: dict[str, str] = {}  # what a running subject's status says
        self.unticked: set[str] = set()
        self.unticked_scans = am.saved_unticked(state.settings)
        # Asked now: the number boxes save what they show once they are up.
        self.first_time = state.settings.value("mriqc_cpus") is None
        self.keys: list[str] = []
        self.runs: list[mq.RunIqms] = []
        self.unreadable: list[str] = []
        self.ratings: dict[str, mq.QcRating] = {}
        self.docker_state, self.docker_message = "", ""  # "" until Docker answers
        self.docker_cpus: int | None = None
        self.docker_gb: float | None = None
        self.probing = False
        self.suggest_pending = False
        self.job: am.MriqcJob | None = None
        # One view is drawn at a time; the last run asked for meanwhile waits as 'wanted'.
        self.drawing = False
        self.wanted: tuple[tuple, Path, str, str] | None = None
        self.shown_key: tuple | None = None
        self.rated: mq.RunIqms | None = None  # whose rating and comment are shown
        self.views: OrderedDict[tuple, bytes | str] = OrderedDict()

    def compose(self) -> ComposeResult:
        settings = self.state.settings
        yield Notices()
        yield FolderBox(
            self.state,
            "mriqc",
            "MRIQC output",
            "Where MRIQC writes: a folder of its own, outside the BIDS and C-PAC folders.",
        )
        yield ImageBox(mq.MRIQC_IMAGE, "the MRIQC image", "~5 GB", settings)
        with Vertical(id="run-part", classes="row"):
            yield DockerLine(id="docker")
            with Horizontal(classes="row"):
                yield Static("CPUs:", classes="label")
                yield Input(str(int(saved_number(settings, "mriqc_cpus", 4))), id="cpus")
                yield Static("Memory (GB):", classes="label")
                yield Input(f"{saved_number(settings, 'mriqc_mem_gb', 8):g}", id="memory")
                yield Button("Suggest", id="suggest")
            yield Line(id="limits", muted=True)
            with Horizontal(classes="row"):
                yield Button("Run MRIQC", id="run", variant="success")
                yield Checkbox("Redo done subjects", id="redo")
            yield Line(id="run-status", muted=True)
            yield RunBox(
                "Running MRIQC", [("stop", "Stop (the running subject is redone next time)")]
            )
            yield Static("Scans to check (space ticks)", classes="section")
            yield TickTable(["Scan"], id="scans")
            yield Static("Subjects to run (space ticks)", classes="section")
            yield TickTable(["Subject", "Sessions", "Scans", "Status"], id="subjects")
            with Horizontal(classes="row"):
                yield Button("Tick all subjects", id="all")
                yield Button("Tick none", id="none")
        yield Static("Results", classes="section")
        with Horizontal(classes="row"):
            yield FilledSelect(
                [(am.MODALITY_TEXT[m], m) for m in mq.MODALITIES],
                id="modality",
                allow_blank=False,
            )
            yield Checkbox("Only runs with outliers", id="flagged-only")
            yield Button("Metric formulas", id="formulas")
        yield Table([], id="results")
        yield Line(id="rating-title")
        with Horizontal(classes="row"):
            for value, meaning in mq.RATINGS.items():
                yield Button(f"{value} {meaning}", id=f"rate-{value}")
            yield Button("No rating", id="rate-0")
        with Horizontal(classes="row"):
            yield Input(placeholder="Comment, optional (Enter saves)", id="comment", classes="wide")
        yield ImagePanel(PICK_A_RUN)
        yield Activity()

    def on_mount(self) -> None:
        self.probed = False  # Docker is first asked when the tab is first shown
        self.state.busy_changed.connect(self.update_controls)
        self.state.folder_changed.connect(lambda _key: self.on_folders())

    def on_folders(self) -> None:
        if self.toolbox.current_page() is self:
            self.show_page()

    @property
    def activity(self) -> Activity:
        return self.query_one(Activity)

    @property
    def run_box(self) -> RunBox:
        return self.query_one(RunBox)

    def results_only(self) -> bool:
        """Only show results: a view-only package, or one that came with MRIQC's results."""
        return VIEW_ONLY in self.state.busy or self.packaged_results

    def modality(self) -> str:
        return self.query_one("#modality", FilledSelect).chosen() or mq.MODALITIES[0]

    def show_page(self) -> None:
        seen = (self.state.generation, self.state.folder("bids"), self.state.folder("mriqc"))
        if seen != self.seen:
            self.seen = seen
            self.fill_scans()
            self.fill_subjects()
            self.load_results()
        if not self.probed and not self.results_only():
            self.probed = True
            self.check_docker()
            self.set_interval(DOCKER_RECHECK_S, self.check_docker)

    # ---- scans and subjects -------------------------------------------------------------------

    def selection(self) -> mq.Selection:
        return am.make_selection(self.keys, self.unticked_scans)

    def fill_scans(self) -> None:
        self.keys = am.scan_keys(self.state.folder("bids"))
        table = self.query_one("#scans", TickTable)
        table.ticked = set(self.keys) - self.unticked_scans
        table.fill([(key, (am.scan_text(key),)) for key in self.keys])

    def fill_subjects(self) -> None:
        bids, output = self.state.folder("bids"), self.state.folder("mriqc")
        selection = self.selection()
        self.subjects, self.statuses = am.subjects_and_states(bids, output, selection)
        self.show_subjects()
        self.update_controls()

    def show_subjects(self) -> None:
        bids, selection = self.state.folder("bids"), self.selection()
        table = self.query_one("#subjects", TickTable)
        table.ticked = set(self.subjects) - self.unticked
        rows = []
        for subject in self.subjects:
            scans = mq.bids_scans(bids, subject, selection) if bids else []
            sessions = len({scan.session for scan in scans} - {""})
            status = self.statuses.get(subject, mq.NOT_RUN)
            color = am.STATUS_COLOR.get(status, ERROR_COLOR)
            text = cell(self.notes.get(subject) or status, color)
            rows.append((subject, (f"sub-{subject}", sessions or "-", len(scans), text)))
        table.fill(rows)

    @on(TickTable.Ticked)
    def on_ticked(self, event: TickTable.Ticked) -> None:
        unticked = set(event.table.row_keys) - event.table.ticked
        if event.table.id == "scans":
            self.unticked_scans = (self.unticked_scans - set(self.keys)) | unticked
            am.save_unticked(self.state.settings, self.unticked_scans)
            self.fill_subjects()
            self.update_notices()
        else:
            self.unticked = unticked
            self.update_controls()

    def ticked(self) -> list[str]:
        return [s for s in self.subjects if s not in self.unticked]

    def to_run(self) -> list[str]:
        redo = self.query_one("#redo", Checkbox).value
        return [s for s in self.ticked() if redo or self.statuses.get(s) != mq.DONE]

    @on(Checkbox.Changed, "#redo")
    def update_controls(self) -> None:
        self.query_one("#run-part").display = not self.results_only()
        running = self.job is not None
        reason = self.state.lock_reason(QUALITY_CHECKING)
        to_run = self.to_run()
        ready = (
            not running
            and not reason
            and self.docker_state == mq.READY
            and self.state.folder("mriqc") is not None
            and bool(to_run)
        )
        self.query_one("#run", Button).disabled = not ready
        self.query_one("#scans", TickTable).locked = running
        box = self.query_one(ImageBox)
        box.lock_reason = "MRIQC is running" if running else ""
        box.update_buttons()
        self.query_one("#run-status", Line).set(
            ""
            if running
            else am.run_status(reason, self.docker_state, len(self.ticked()), len(to_run))
        )

    def update_notices(self) -> None:
        self.query_one(Notices).set_messages(
            am.notices(
                self.state,
                self.keys,
                self.selection().empty or not self.subjects,
                self.runs,
                self.unreadable,
                self.modality(),
                self.results_only(),
                self.docker_message,
            )
        )
        present = {mq.READY: True, mq.IMAGE_MISSING: False}.get(self.docker_state)
        self.query_one(ImageBox).set_present(None if self.results_only() else present)
        line = am.DOCKER_LINE.get(self.docker_state, (None, "", ""))
        self.query_one(DockerLine).show_state(*line)

    # ---- Docker -------------------------------------------------------------------------------

    def check_docker(self) -> None:
        if self.probing:
            return
        self.probing = True
        self.update_notices()
        self.start_job(
            am.probe_docker,
            lambda result: self.on_probed(*result),
            lambda error: self.on_probed(mq.DOCKER_STOPPED, error, None, None),
        )

    def on_probed(self, state: str, message: str, cpus: int | None, mem_gb: float | None) -> None:
        self.probing = False
        self.docker_state, self.docker_message = state, message
        self.docker_cpus, self.docker_gb = cpus, mem_gb
        if self.suggest_pending or (self.first_time and cpus is not None):
            self.fill_in_suggestion()
            self.first_time = False
        self.suggest_pending = False
        self.update_limits()
        self.update_notices()
        self.update_controls()

    @on(Button.Pressed, "#suggest")
    def on_suggest(self) -> None:
        """Fill in from the limits known now, then again once Docker answers afresh."""
        self.fill_in_suggestion()
        self.suggest_pending = True
        self.check_docker()

    def fill_in_suggestion(self) -> None:
        cpus, mem_gb = mq.suggest_resources(self.docker_cpus, self.docker_gb)
        self.query_one("#cpus", Input).value = str(cpus)
        self.query_one("#memory", Input).value = f"{mem_gb:g}"

    def resources(self) -> tuple[int, float] | None:
        cpus = whole_number(self.query_one("#cpus", Input).value, 1, self.docker_cpus or MAX_CPUS)
        memory = decimal(self.query_one("#memory", Input).value, mq.MIN_MEM_GB, MAX_MEM_GB)
        return (cpus, memory) if cpus is not None and memory is not None else None

    @on(Input.Changed, "#cpus")
    @on(Input.Changed, "#memory")
    def update_limits(self) -> None:
        chosen = self.resources()
        if chosen is not None:
            self.state.settings.setValue("mriqc_cpus", chosen[0])
            self.state.settings.setValue("mriqc_mem_gb", chosen[1])
        self.query_one("#limits", Line).set(
            am.limits_text(self.docker_cpus, self.docker_gb, chosen or (0, 0.0))
        )

    @on(ImageBox.Finished)
    def on_image(self, event: ImageBox.Finished) -> None:
        self.activity.say(event.text, event.error)
        self.check_docker()

    # ---- running ------------------------------------------------------------------------------

    @on(Button.Pressed, "#run")
    def on_run(self) -> None:
        bids, output = self.state.folder("bids"), self.state.folder("mriqc")
        subjects, resources = self.to_run(), self.resources()
        if bids is None or output is None or not subjects or self.job is not None:
            return
        if resources is None:
            most = self.docker_cpus or MAX_CPUS
            self.show_text(
                "CPUs and memory",
                f"CPUs: a whole number from 1 to {most}. Memory: at least {mq.MIN_MEM_GB:g} GB.",
            )
            return
        state, message, docker = mq.docker_status()
        if docker is None or state != mq.READY:
            self.docker_state, self.docker_message = state, message
            self.update_notices()
            self.update_controls()
            return
        self.job = am.MriqcJob(
            docker, bids, output, subjects, *resources, self.selection(), Listener(self)
        )
        self.state.set_busy(QUALITY_CHECKING, True)
        self.activity.say(f"MRIQC on {len(subjects)} subject(s), one at a time.")
        self.run_box.start(len(subjects), None, "Starting MRIQC...")
        self.update_controls()
        self.start_job(self.job.run, lambda _result: None, self.all_done)

    def scans_of(self, subject: str) -> int:
        return am.subject_scans(self.state.folder("bids"), subject, self.selection())

    def subject_started(self, subject: str) -> None:
        batch = self.run_box.progress
        batch.estimate = am.steps_estimate(self.state.settings, self.scans_of(subject))
        batch.subject_started(subject)
        self.run_box.show_progress()
        self.set_status(subject, am.RUNNING)

    def step(self, subject: str, steps: int, node: str) -> None:
        self.run_box.progress.step(subject, steps, node)  # shown by the box's own timer

    def subject_done(self, subject: str, failure: str) -> None:
        batch = self.run_box.progress
        progress, scans = batch.running.get(subject), self.scans_of(subject)
        if not failure and progress is not None and progress.steps and scans:
            self.state.settings.setValue("mriqc_steps_per_scan", progress.steps / scans)
        batch.subject_finished(subject)
        bids, output = self.state.folder("bids"), self.state.folder("mriqc")
        status = mq.DONE
        if bids is not None:
            status = mq.subject_state(bids, output, subject, self.selection())
        self.set_status(subject, am.FAILED if failure else status)
        self.load_results()

    def set_status(self, subject: str, status: str) -> None:
        self.statuses[subject] = status
        self.show_subjects()

    def all_done(self, summary: str) -> None:
        self.job = None
        self.run_box.finish()
        self.state.set_busy(QUALITY_CHECKING, False)
        self.activity.say(summary)
        failed = {s for s, status in self.statuses.items() if status == am.FAILED}
        self.fill_subjects()
        for subject in failed & set(self.subjects):
            self.set_status(subject, am.FAILED)
        self.load_results()

    @on(Button.Pressed, "#stop")
    def on_stop(self) -> None:
        if self.job is not None:
            self.activity.say("Stopping MRIQC...")
            self.run_box.set_stage("Stopping...")
            self.job.stop()

    # ---- results ------------------------------------------------------------------------------

    @on(Select.Changed, "#modality")
    def load_results(self) -> None:
        output = self.state.folder("mriqc")
        self.runs, self.unreadable, self.ratings = [], [], {}
        if output is not None and output.is_dir():
            bids = self.state.folder("bids")
            self.runs, self.unreadable = mq.collect_iqms(output, self.modality(), bids)
            self.ratings = mq.read_ratings(output)
        self.fill_results()
        self.update_notices()

    @on(Checkbox.Changed, "#flagged-only")
    def fill_results(self) -> None:
        modality = self.modality()
        only = self.query_one("#flagged-only", Checkbox).value
        flags = mq.flag_outliers(self.runs)
        table = self.query_one("#results", Table)
        metrics = [m.label for m in mq.METRICS[modality]]
        columns = ["Subject", "Session", "Run", "QC rating", "Flags", *metrics]
        if table.names != columns:
            table.set_columns(columns)
        rows: list[tuple[str, list[object]]] = []
        for subject, sessions in mq.by_subject_session(self.runs).items():
            for session, runs in sessions.items():
                for run in runs:
                    flagged = flags.get(run.name, set())
                    if not only or flagged:
                        rows.append((run.name, self.result_cells(subject, session, run, flagged)))
        table.fill(rows)
        self.show_run()

    def result_cells(
        self, subject: str, session: str, run: mq.RunIqms, flagged: set[str]
    ) -> list[object]:
        rating = self.ratings.get(run.name)
        color = am.RATING_COLORS.get(rating.rating or 0) if rating else None
        shown = am.rating_text(rating)
        note = am.flags_text(run, flagged)
        metrics = [
            on_background(text, am.OUTLIER_BACKGROUND) if key in flagged else cell(text)
            for key, text in am.metric_cells(run, self.modality())
        ]
        return [
            f"sub-{subject}",
            f"ses-{session}" if session else "-",
            run.label,
            on_background(shown, color) if color else cell(shown),
            on_background(note, am.STALE_BACKGROUND) if run.state == mq.STALE else Text(note),
            *metrics,
        ]

    @on(Button.Pressed, "#formulas")
    def formulas(self) -> None:
        modality = self.modality()
        title = f"How MRIQC's {am.MODALITY_TEXT[modality]} metrics are calculated"
        self.show_text(title, "\n".join(help_lines(MRIQC_DOCS[modality])))

    # ---- the highlighted run: its view and rating ---------------------------------------------

    def current(self) -> mq.RunIqms | None:
        key = self.query_one("#results", Table).current_key()
        return next((r for r in self.runs if r.name == key), None)

    @on(Table.RowHighlighted, "#results")
    def show_run(self) -> None:
        run = self.current()
        self.show_rating(run)
        panel = self.query_one(ImagePanel)
        if run is None:
            self.shown_key = None
            panel.show_message(PICK_A_RUN)
            return
        bids = self.state.folder("bids")
        path = bids / run.nifti if bids is not None and run.nifti is not None else None
        try:
            mtime = path.stat().st_mtime if path is not None else None
        except OSError:
            mtime = None
        if path is None or mtime is None:
            self.shown_key = None
            panel.show_message(
                f"{run.name}\n\nThe BIDS scan isn't here, so it can't be shown: rate it from "
                "the metrics above."
            )
            return
        key = (str(path), mtime)
        if key == self.shown_key:
            return
        self.shown_key = key
        if key in self.views:
            self.views.move_to_end(key)
            self.show_view(self.views[key], run.name)
            return
        panel.show_message(f"Drawing {path.name}...")
        self.wanted = (key, path, run.modality, run.name)
        self.draw_next()

    def draw_next(self) -> None:
        if self.drawing or self.wanted is None:
            return  # one at a time: the wanted one starts when this finishes
        key, path, modality, name = self.wanted
        self.wanted = None
        self.drawing = True
        self.start_job(
            lambda: am.view_png(path, modality),
            lambda result: self.on_drawn(key, name, *result),
            lambda error: self.on_drawn(key, name, None, error),
        )

    def on_drawn(self, key: tuple, name: str, data: bytes | None, error: str) -> None:
        self.drawing = False
        self.views[key] = data if data is not None else error
        while len(self.views) > VIEW_CACHE_SIZE:
            self.views.popitem(last=False)
        if key == self.shown_key:
            self.show_view(self.views[key], name)
        self.draw_next()

    def show_view(self, view: bytes | str, name: str) -> None:
        panel = self.query_one(ImagePanel)
        if isinstance(view, str):
            panel.show_message(view)
        else:
            panel.show_png(view, name)

    def show_rating(self, run: mq.RunIqms | None) -> None:
        self.rated = run
        rating = self.ratings.get(run.name) if run else None
        enabled = run is not None and self.state.folder("mriqc") is not None
        for value in (*mq.RATINGS, 0):
            button = self.query_one(f"#rate-{value}", Button)
            button.disabled = not enabled
            chosen = rating is not None and rating.rating == value
            button.variant = "primary" if chosen else "default"
        comment = self.query_one("#comment", Input)
        comment.disabled = not enabled
        comment.value = rating.comment if rating else ""
        title = f"QC rating of {run.name}: {am.rating_text(rating) or 'none yet'}" if run else ""
        if rating is not None and rating.rater:
            title += f" (by {rating.rater})"
        self.query_one("#rating-title", Line).set(title)

    @on(Button.Pressed)
    def on_rate(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("rate-"):
            value = int(button_id.removeprefix("rate-"))
            self.save_rating(value or None, self.query_one("#comment", Input).value)
        elif button_id in ("all", "none"):
            self.query_one("#subjects", TickTable).tick_all(button_id == "all")

    @on(Input.Submitted, "#comment")
    @on(Input.Blurred, "#comment")
    def on_comment(self, event: Input.Submitted | Input.Blurred) -> None:
        run = self.rated
        rating = self.ratings.get(run.name) if run else None
        self.save_rating(rating.rating if rating else None, event.value)

    def save_rating(self, rating: int | None, comment: str) -> None:
        run, output = self.rated, self.state.folder("mriqc")
        comment = comment.strip()
        if run is None or output is None:
            return
        if not am.rating_changed(self.ratings.get(run.name), rating, comment):
            return
        try:
            saved = mq.save_rating(output, run.name, rating, comment)
        except OSError as e:
            self.show_text("Rating not saved", f"Couldn't write the ratings file: {e}")
            return
        if saved is None:
            self.ratings.pop(run.name, None)
        else:
            self.ratings[run.name] = saved
        self.fill_results()
