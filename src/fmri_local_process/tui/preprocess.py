"""Preprocess BIDS (C-PAC): tick tasks and subjects, run C-PAC in Docker, and look at each subject's
outputs."""

import os
import zlib
from datetime import datetime

from nibabel.filebasedimages import ImageFileError
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, HorizontalScroll, Vertical
from textual.widgets import Button, Checkbox, Input, Select, Static

from fmri_local_process.app import figures
from fmri_local_process.app import preprocess as ap
from fmri_local_process.app.colors import RUNNING_COLOR
from fmri_local_process.app.output_help import MOTION_HELP, qc_figure_help, slice_help
from fmri_local_process.app.state import PREPROCESSING, VIEW_ONLY, ToolboxState
from fmri_local_process.processing.brain_slices import (
    SlicePreview,
    render_slices,
    slice_previews,
)
from fmri_local_process.processing.cpac_docker import (
    CPAC_IMAGE,
    HANG_ACTIONS,
    MIN_MEM_GB,
    DockerUnavailableError,
    HangPolicy,
    ParallelPlan,
    bids_tasks,
    bold_sessions,
    host_memory_gb,
    preprocessed_bolds,
)
from fmri_local_process.processing.cpac_outputs import QcFigure, motion_summaries, qc_figures
from fmri_local_process.processing.session_sync import session_changes
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
    whole_number,
)

DOCKER_RECHECK_S = 30
MAX_MEM_GB = 512.0
MAX_HANG_MINUTES = 24 * 60
MAX_RESTARTS = 10
REMOVE_NOTE = (
    "The T1w defacer image (if set up) is built on C-PAC and keeps most of its layers, so less "
    "space is freed while it is there."
)


class Listener:
    """The job's calls (on its threads), passed on to the page on the UI thread."""

    def __init__(self, page: "PreprocessPage") -> None:
        self.page = page

    def on_message(self, text: str, is_error: bool, /) -> None:
        self.page.tell(self.page.say, text, is_error)

    def on_subject_started(self, index: int, total: int, subject: str, /) -> None:
        self.page.tell(self.page.subject_started, subject)

    def on_step(self, subject: str, node: str, steps_done: int, /) -> None:
        self.page.tell(self.page.run_box.progress.step, subject, steps_done, node)

    def on_subject_finished(self, subject: str, steps: int, preprocessed: bool, /) -> None:
        self.page.tell(self.page.subject_finished, subject, steps, preprocessed)

    def on_batch_done(self, result: ap.RunSummary | str, /) -> None:
        self.page.tell(self.page.batch_done, result)


class PreprocessPage(Page):
    def __init__(self, state: ToolboxState) -> None:
        super().__init__(state)
        self.settings = state.make_settings(ap.SETTINGS_APP)
        self.choices = ap.read_choices(self.settings)
        self.unticked_tasks = set(self.choices.unticked_tasks)
        self.unticked: set[str] = set()  # subjects; new ones start ticked
        self.labels: list[str] = []  # the BIDS folder's tasks
        self.subjects: list[str] = []
        self.statuses: dict[str, str] = {}
        self.seen: tuple = ()
        self.probe = ap.DockerProbe()
        self.probed = self.probing = self.suggest_pending = False
        self.host_gb = host_memory_gb()
        self.job: ap.PreprocessJob | None = None
        self.figures: list[QcFigure] = []
        self.slices: list[SlicePreview] = []
        self.shown: str | None = None  # the subject whose outputs are shown

    def help_text(self) -> str:
        return (
            "Runs C-PAC's standard 'preproc' pipeline in Docker, one container per subject.\n\n"
            f"Status\n{ap.STATUS_LEGEND}\n\n"
            "Run mode. Skip done: subjects with every expected output are skipped; incomplete "
            "ones pick up where they stopped. Resume all: runs every ticked subject, reusing "
            "finished steps. Rerun from scratch: deletes the subject's working and output "
            "folders first. Keep CPUs the same when resuming, or C-PAC may redo the BOLD steps.\n\n"
            "If stuck: a subject counts as hung when no step starts or finishes and its "
            "container's CPU stays near idle for this long (0: never check).\n\n"
            f"Motion\n{MOTION_HELP}"
        )

    def compose(self) -> ComposeResult:
        saved = self.choices
        yield Notices()
        yield FolderBox(self.state, "output", "C-PAC output folder", "Where C-PAC writes.")
        yield ImageBox(CPAC_IMAGE, "the C-PAC image", "~15 GB", self.settings, REMOVE_NOTE)
        yield DockerLine(id="docker")
        with Horizontal(classes="row"):
            yield Static("Subjects at once:", classes="label")
            yield Input(str(saved.plan.parallel), id="parallel")
            yield Static("CPUs each:", classes="label")
            yield Input(str(saved.plan.n_cpus), id="cpus")
            yield Static("GB each:", classes="label")
            yield Input(f"{saved.plan.mem_gb:g}", id="memory")
            yield Button("Suggest", id="suggest")
        yield Line(id="memory-text")
        with Horizontal(classes="row"):
            yield Static("Run mode:", classes="label")
            yield FilledSelect(
                [(text, mode) for mode, text in ap.RUN_MODES.items()],
                value=saved.mode,
                id="mode",
                allow_blank=False,
            )
            yield Checkbox("Keep C-PAC's working folder", saved.keep_working, id="keep")
        with Horizontal(classes="row"):
            yield Static("If stuck for (minutes):", classes="label")
            yield Input(f"{saved.hang.minutes:g}", id="hang")
            yield FilledSelect(
                [(text, action) for action, text in HANG_ACTIONS.items()],
                value=saved.hang.action if saved.hang.action in HANG_ACTIONS else "restart",
                id="hang-action",
                allow_blank=False,
            )
            yield Static("restarts:", classes="label")
            yield Input(str(saved.hang.max_restarts), id="restarts")
        with Horizontal(classes="row"):
            yield Button("Preprocess", id="process", variant="success")
            yield Button("Refresh", id="refresh")
        yield Line(id="lock")
        yield RunBox(
            "Running C-PAC",
            [
                ("stop", "Stop after running subject(s)"),
                ("stop-now", "Stop now (free CPU and memory)"),
            ],
        )
        yield Static("Tasks (space ticks)", classes="section")
        yield TickTable(["Task"], id="tasks")
        yield Static("Subjects (space ticks)", classes="section")
        yield TickTable(["Subject", "Sessions", "Status"], id="subjects")
        with Horizontal(classes="row"):
            yield Button("Tick all", id="all")
            yield Button("Tick none", id="none")
        yield Line(id="summary", muted=True)
        yield Activity()
        with Vertical(id="detail", classes="row"):
            yield Line(id="detail-title")
            yield Line(id="problems")
            with Horizontal(classes="row"):
                yield Button("Rename output to the new session numbers...", id="rename")
            yield Line(id="bolds")
            yield Static("QC figures", classes="section")
            with Horizontal(classes="row"):
                yield FilledSelect([], prompt="No QC figures yet", id="figure")
                yield Button("What to look for", id="figure-help")
            yield ImagePanel("No QC figures for this subject yet.", id="figure-image")
            yield Static("Brain slices", classes="section")
            with Horizontal(classes="row"):
                yield FilledSelect([], prompt="No brain images yet", id="slice")
                yield Button("What to look for", id="slice-help")
            yield ImagePanel("No brain images for this subject yet.", id="slice-image")
            yield Static("Motion", classes="section")
            yield Table(["Run"], id="motion")
            yield Static("C-PAC log (its end)", classes="section")
            with HorizontalScroll(classes="row"):
                yield Static(id="cpac-log", markup=False)

    def on_mount(self) -> None:
        self.state.busy_changed.connect(self.update_controls)
        self.state.folder_changed.connect(self.on_folder_changed)
        self.query_one("#cpac-log").styles.width = "auto"

    def on_folder_changed(self, key: str) -> None:
        if key in ("bids", "output") and self.toolbox.current_page() is self:
            self.show_page()

    @property
    def activity(self) -> Activity:
        return self.query_one(Activity)

    @property
    def run_box(self) -> RunBox:
        return self.query_one(RunBox)

    def say(self, text: str, error: bool = False) -> None:
        self.activity.say(f"[{datetime.now():%H:%M:%S}] {text}", error)

    def show_page(self) -> None:
        seen = (self.state.generation, self.state.folder("bids"), self.state.folder("output"))
        if self.seen != seen:
            self.seen = seen
            self.fill_tasks()
            self.refresh_roster()
        if not self.probed and VIEW_ONLY not in self.state.busy:
            self.probed = True
            self.check_docker()
            self.set_interval(DOCKER_RECHECK_S, self.check_docker)

    # ---- tasks and subjects -------------------------------------------------------------------

    def fill_tasks(self) -> None:
        bids = self.state.folder("bids")
        self.labels = bids_tasks(bids) if bids else []
        table = self.query_one("#tasks", TickTable)
        table.ticked = set(self.labels) - self.unticked_tasks
        table.fill([(label, (label,)) for label in self.labels])

    def tasks(self) -> list[str]:
        return ap.task_filter(self.labels, self.unticked_tasks)

    def any_task(self) -> bool:
        return any(label not in self.unticked_tasks for label in self.labels)

    def running(self) -> set[str]:
        return set(self.run_box.progress.running) if self.job is not None else set()

    def status_of(self, subject: str) -> str:
        bids, output = self.state.folder("bids"), self.state.folder("output")
        return ap.subject_status(bids, output, subject, self.tasks(), subject in self.running())

    @on(Button.Pressed, "#refresh")
    def refresh_roster(self) -> None:
        bids, output = self.state.folder("bids"), self.state.folder("output")
        self.subjects = ap.roster(bids, output, self.tasks(), self.any_task())
        self.statuses = {s: self.status_of(s) for s in self.subjects}
        self.show_roster()
        self.shown = None  # its outputs may have changed
        self.show_detail()

    def show_roster(self) -> None:
        bids = self.state.folder("bids")
        table = self.query_one("#subjects", TickTable)
        table.ticked = set(self.subjects) - self.unticked
        rows = []
        for subject in self.subjects:
            sessions = len(bold_sessions(bids, subject, self.tasks())) if bids else 0
            status = self.statuses[subject]
            icon, color = ap.STATUS_ICON[status]
            rows.append((subject, (f"sub-{subject}", sessions, cell(f"{icon} {status}", color))))
        table.fill(rows)
        self.update_notices()
        self.update_controls()

    def update_notices(self) -> None:
        bids, output = self.state.folder("bids"), self.state.folder("output")
        missing = self.probe.has_image is False
        self.query_one(Notices).set_messages(
            ap.notices(bids, output, self.labels, self.any_task(), missing)
        )
        self.query_one("#summary", Line).set(
            ap.summary_text(bids, output, self.tasks(), self.any_task(), self.statuses)
        )

    @on(TickTable.Ticked)
    def on_ticked(self, event: TickTable.Ticked) -> None:
        unticked = set(event.table.row_keys) - event.table.ticked
        if event.table.id == "tasks":
            self.unticked_tasks = (self.unticked_tasks - set(self.labels)) | unticked
            ap.save_unticked_tasks(self.settings, self.unticked_tasks)
            self.refresh_roster()
        else:
            self.unticked = unticked
            self.update_controls()

    def ticked(self) -> list[str]:
        return [s for s in self.subjects if s not in self.unticked]

    def update_controls(self) -> None:
        idle = self.job is None
        for table_id in ("#tasks", "#subjects"):
            self.query_one(table_id, TickTable).locked = not idle
        lock = self.state.lock_reason(PREPROCESSING)
        box = self.query_one(ImageBox)
        box.lock_reason = "" if idle else "C-PAC is running"
        box.update_buttons()
        no_image = self.probe.has_image is False or box.busy
        ticked = self.ticked()
        button = self.query_one("#process", Button)
        button.label = f"Preprocess {len(ticked)} ticked subject(s)"
        output = self.state.folder("output")
        button.disabled = not (idle and ticked and output and not lock and not no_image)
        reason = lock or ("Download the C-PAC image first" if no_image else "")
        self.query_one("#lock", Line).set(f"Locked: {reason}." if reason else "", "#f39c12")
        self.query_one("#rename", Button).disabled = not idle or bool(lock)
        self.update_memory()

    # ---- Docker and the plan ------------------------------------------------------------------

    def check_docker(self) -> None:
        if self.probing:
            return
        self.probing = True
        self.start_job(
            ap.probe_docker,
            self.on_probed,
            lambda error: self.on_probed(ap.DockerProbe(error=error)),
        )

    def on_probed(self, probe: ap.DockerProbe) -> None:
        self.probing = False
        self.probe = probe
        warning = (
            "Docker is running, but the C-PAC image isn't downloaded yet."
            if probe.has_image is False
            else ""
        )
        self.query_one(DockerLine).show_state(not probe.error, probe.error, warning)
        self.query_one(ImageBox).set_present(probe.has_image)
        if self.suggest_pending and probe.gb is not None:
            self.fill_in_suggestion()
        self.suggest_pending = False
        self.update_notices()
        self.update_controls()

    def plan(self) -> ParallelPlan | None:
        """What is typed; None when some of it isn't a number in range."""
        cpus = os.cpu_count() or 1
        parallel = whole_number(self.query_one("#parallel", Input).value, 1, max(1, cpus // 2))
        n_cpus = whole_number(self.query_one("#cpus", Input).value, 1, cpus)
        memory = decimal(self.query_one("#memory", Input).value, MIN_MEM_GB, MAX_MEM_GB)
        if parallel is None or n_cpus is None or memory is None:
            return None
        return ParallelPlan(parallel, n_cpus, memory)

    @on(Input.Changed, "#parallel")
    @on(Input.Changed, "#cpus")
    @on(Input.Changed, "#memory")
    def update_memory(self) -> None:
        plan, line = self.plan(), self.query_one("#memory-text", Line)
        if plan is None:
            cpus = os.cpu_count() or 1
            line.set(
                f"Subjects at once: 1 to {max(1, cpus // 2)}; CPUs: 1 to {cpus}; memory: at least "
                f"{MIN_MEM_GB:g} GB.",
                "#e74c3c",
            )
        elif self.probed and not self.probing:
            line.set(*ap.memory_text(plan, self.probe, self.host_gb, len(self.ticked())))

    @on(Button.Pressed, "#suggest")
    def on_suggest(self) -> None:
        """Fill in from the limits known now, then again once Docker is re-asked."""
        self.fill_in_suggestion()
        self.suggest_pending = True
        self.check_docker()

    def fill_in_suggestion(self) -> None:
        plan = ap.suggested_plan(self.probe, len(self.ticked()))
        self.query_one("#parallel", Input).value = str(plan.parallel)
        self.query_one("#cpus", Input).value = str(plan.n_cpus)
        self.query_one("#memory", Input).value = f"{plan.mem_gb:g}"

    @on(ImageBox.Finished)
    def on_image(self, event: ImageBox.Finished) -> None:
        self.say(event.text, event.error)
        self.check_docker()

    # ---- running ------------------------------------------------------------------------------

    def hang_policy(self) -> HangPolicy | None:
        minutes = whole_number(self.query_one("#hang", Input).value, 0, MAX_HANG_MINUTES)
        restarts = whole_number(self.query_one("#restarts", Input).value, 0, MAX_RESTARTS)
        if minutes is None or restarts is None:
            return None
        action = self.query_one("#hang-action", FilledSelect).chosen() or "restart"
        return HangPolicy(float(minutes), action, restarts)

    @on(Button.Pressed, "#process")
    def on_process(self) -> None:
        bids, output = self.state.folder("bids"), self.state.folder("output")
        subjects, plan, hang = self.ticked(), self.plan(), self.hang_policy()
        if bids is None or output is None or not subjects or self.job is not None:
            return
        if self.probe.error:
            self.show_text("Docker isn't running", f"{self.probe.error}\n\nStart it, then retry.")
            self.check_docker()
            return
        if plan is None or hang is None:
            self.show_text("Check the numbers", "A number above isn't in its range.")
            return
        mode = self.query_one("#mode", FilledSelect).chosen() or "skip"
        run = ap.RunSettings(
            bids_root=bids,
            output_root=output,
            subjects=subjects,
            tasks=self.tasks(),
            n_cpus=plan.n_cpus,
            mem_gb=plan.mem_gb,
            mode=mode,
            parallel=min(plan.parallel, len(subjects)),
            hang=hang,
            keep_working=self.query_one("#keep", Checkbox).value,
        )

        def start() -> None:
            self.start_run(run, plan)

        def check_plan() -> None:
            if question := ap.overload_question(plan, self.probe, len(subjects)):
                self.ask("More than Docker allows?", question, start)
            else:
                start()

        if mode == "scratch":
            self.ask("Rerun from scratch?", ap.scratch_question(subjects), check_plan)
        else:
            check_plan()

    def start_run(self, run: ap.RunSettings, plan: ParallelPlan) -> None:
        if self.job is not None or self.state.lock_reason():
            return
        ap.save_choices(self.settings, run, plan)
        try:
            run.output_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.show_text("Preprocessing not started", str(e))
            return
        self.job = ap.PreprocessJob(run, Listener(self))
        self.say(ap.start_text(run))
        estimate = ap.saved_estimate(self.settings, run.tasks)
        self.run_box.start(len(run.subjects), estimate, "Checking Docker...")
        self.query_one("#stop", Button).disabled = False
        self.query_one("#stop-now", Button).disabled = False
        self.state.set_busy(PREPROCESSING, True)
        self.start_job(self.job.run, lambda _result: None, self.batch_done)

    def subject_started(self, subject: str) -> None:
        batch = self.run_box.progress
        if not batch.running:  # re-read: the last subject may have just saved a new count
            batch.estimate = ap.saved_estimate(self.settings, self.tasks())
        batch.subject_started(subject)
        self.statuses[subject] = "running"
        self.show_roster()

    def subject_finished(self, subject: str, steps: int, preprocessed: bool) -> None:
        if preprocessed and steps:
            self.settings.setValue(ap.estimate_key(self.tasks()), steps)
        self.run_box.progress.subject_finished(subject)
        self.statuses[subject] = self.status_of(subject)
        self.show_roster()
        if self.shown == subject:
            self.shown = None
            self.show_detail()

    def batch_done(self, result: ap.RunSummary | str) -> None:
        self.job = None
        self.run_box.finish()
        self.state.set_busy(PREPROCESSING, False)
        if isinstance(result, str):
            self.say(result, error=True)
            self.show_text("Preprocessing not started", result)
        else:
            for line, is_error in ap.done_lines(result):
                self.say(line, is_error)
        self.refresh_roster()

    @on(Button.Pressed, "#stop")
    def on_stop(self) -> None:
        if self.job is not None:
            self.job.stop()
            self.query_one("#stop", Button).disabled = True
            self.say("Stopping once the running subject(s) finish...")

    @on(Button.Pressed, "#stop-now")
    def on_stop_now(self) -> None:
        job = self.job
        if job is None:
            return

        def stop() -> None:
            self.query_one("#stop", Button).disabled = True
            self.query_one("#stop-now", Button).disabled = True
            self.run_box.set_stage("Stopping...")
            self.say("Stopping the running C-PAC container(s) now...")
            self.start_job(job.stop_now, lambda _stopped: None)  # docker takes ~10 s

        self.ask("Stop now?", ap.stop_now_question(len(self.running())), stop)

    def stop_for_quit(self) -> None:
        """Blocks on docker (~10 s): off the UI thread."""
        job = self.job
        if job is not None:
            ap.mark_all_stopped(job.run_settings.output_root, job.stop_now())

    # ---- one subject's outputs ----------------------------------------------------------------

    @on(Table.RowHighlighted, "#subjects")
    def show_detail(self) -> None:
        subject = self.query_one("#subjects", TickTable).current_key()
        bids, output = self.state.folder("bids"), self.state.folder("output")
        self.query_one("#detail").display = subject is not None and output is not None
        if subject is None or output is None or subject == self.shown:
            return
        self.shown = subject
        self.query_one("#detail-title", Line).set(f"sub-{subject}", RUNNING_COLOR)
        status, tasks = self.statuses.get(subject), self.tasks()
        problems = []
        if status in ("failed", "stopped"):
            problems.append(ap.failure_problem(output, subject, status == "stopped"))
        if status == "partial":
            to_do = ap.tasks_to_do(bids, output, subject, tasks)
            problems.append(ap.partial_problem(to_do))
        self.query_one("#rename").display = status == "renumbered"
        if status == "renumbered" and bids is not None:
            problems.append(ap.renumbered_problem(session_changes(bids, output, subject)))
        if bids is not None and status != "running":
            missing = ap.missing_problem(bids, output, subject, tasks)
            if missing is not None:
                problems.append(missing)
        texts = [
            "\n".join([f"⚠ {p.title}", p.text, *(f"   {x}" for x in p.listing)]) for p in problems
        ]
        self.query_one("#problems", Line).set(
            "\n\n".join(texts), problems[0].color if problems else ""
        )
        bolds = preprocessed_bolds(output, subject)
        lines = [f"Preprocessed BOLD ({len(bolds)})", *(f"   {b}" for b in bolds)]
        self.query_one("#bolds", Line).set("\n".join(lines))
        self.figures = qc_figures(output, subject, tasks)
        self.slices = slice_previews(output, subject, tasks)
        self.query_one("#figure", FilledSelect).fill(
            [(f.label, str(i)) for i, f in enumerate(self.figures)], prefer="0"
        )
        self.query_one("#slice", FilledSelect).fill(
            [(s.label, str(i)) for i, s in enumerate(self.slices)], prefer="0"
        )
        self.show_figure()
        self.show_slice()
        self.fill_motion(motion_summaries(output, subject, tasks))
        self.query_one("#cpac-log", Static).update(ap.log_text(output, subject))

    def current_figure(self) -> QcFigure | None:
        key = self.query_one("#figure", FilledSelect).chosen()
        return self.figures[int(key)] if key is not None and self.figures else None

    def current_slice(self) -> SlicePreview | None:
        key = self.query_one("#slice", FilledSelect).chosen()
        return self.slices[int(key)] if key is not None and self.slices else None

    @on(Select.Changed, "#figure")
    def show_figure(self) -> None:
        figure, panel = self.current_figure(), self.query_one("#figure-image", ImagePanel)
        self.query_one("#figure-help", Button).disabled = figure is None
        if figure is None:
            panel.show_message("No QC figures for this subject yet.")
        else:
            panel.show_file(figure.path)

    @on(Select.Changed, "#slice")
    def show_slice(self) -> None:
        preview, panel = self.current_slice(), self.query_one("#slice-image", ImagePanel)
        self.query_one("#slice-help", Button).disabled = preview is None
        if preview is None:
            panel.show_message("No brain images for this subject yet.")
            return
        panel.show_message(f"Drawing {preview.label}...")

        def draw() -> bytes | str:
            try:
                return figures.png_bytes(render_slices(preview))
            except (OSError, EOFError, ValueError, zlib.error, ImageFileError) as e:
                return f"Couldn't read {preview.path.name}:\n{e}"

        def drawn(result: bytes | str) -> None:
            if preview != self.current_slice():
                return  # another one was chosen meanwhile
            if isinstance(result, str):
                panel.show_message(result)
            else:
                panel.show_png(result, f"{preview.path.name}_{preview.kind}")

        self.start_job(draw, drawn, drawn)

    @on(Button.Pressed, "#figure-help")
    def figure_help(self) -> None:
        figure = self.current_figure()
        if figure is not None:
            self.show_text(figure.label, qc_figure_help(figure.desc, figure.reg))

    @on(Button.Pressed, "#slice-help")
    def on_slice_help(self) -> None:
        preview = self.current_slice()
        if preview is not None:
            self.show_text(preview.label, slice_help(preview.kind))

    def fill_motion(self, tables: list) -> None:
        columns, rows = ap.motion_rows(tables)
        table = self.query_one("#motion", Table)
        if table.names != ["Run", *columns]:
            table.set_columns(["Run", *columns])
        table.fill(
            [
                (str(i), (run, *(values.get(name, "") for name in columns)))
                for i, (run, values) in enumerate(rows)
            ]
        )

    @on(Button.Pressed, "#rename")
    def on_rename(self) -> None:
        subject = self.query_one("#subjects", TickTable).current_key()
        bids, output = self.state.folder("bids"), self.state.folder("output")
        if subject is None or bids is None or output is None:
            return
        changes = session_changes(bids, output, subject)

        def rename() -> None:
            self.start_job(work, done)

        def work() -> int | str:
            try:
                return ap.rename_subject(bids, output, subject, changes)
            except DockerUnavailableError as e:
                return f"Start Docker first: {e}"
            except OSError as e:
                return str(e)

        def done(result: int | str) -> None:
            if isinstance(result, str):
                self.show_text("Not renamed", result)
                return
            self.say(f"sub-{subject}: renamed {result} output path(s) to the new session numbers")
            self.refresh_roster()

        self.ask("Rename output", ap.rename_question(subject, changes), rename)

    @on(Button.Pressed)
    def on_tick_button(self, event: Button.Pressed) -> None:
        if event.button.id in ("all", "none"):
            self.query_one("#subjects", TickTable).tick_all(event.button.id == "all")
