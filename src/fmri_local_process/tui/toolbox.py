"""fmri_tui: the toolbox in a terminal, for servers without a display (over SSH). The same tabs
in the same order as the window, on the same app/ layer; nothing here imports Qt."""

import os
import shutil
import sys
import threading

import click
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Static, TabbedContent, TabPane

from fmri_local_process.app.figures import figures_dir
from fmri_local_process.app.settings import MakeSettings, json_settings
from fmri_local_process.app.state import (
    CONVERTING,
    PREPROCESSING,
    TRIMMING,
    VIEW_ONLY,
    ToolboxState,
)
from fmri_local_process.app.tabs import (
    BEHAV,
    CROSSCHECK,
    FIRST_LEVEL,
    MRIQC,
    OVERVIEW,
    PREPROCESS,
    SCAN_LENGTHS,
    SEQUENCE,
    SESSIONS,
    SETUP,
    TAB_INTROS,
    TAB_ORDER,
    header_texts,
    tab_names,
    tab_text,
    title_text,
)
from fmri_local_process.processing.review_mode import view_only
from fmri_local_process.tui import images
from fmri_local_process.tui.behav import BehavPage, BehavShared, SessionsPage
from fmri_local_process.tui.convert import ConvertPage
from fmri_local_process.tui.crosscheck import CrosscheckPage
from fmri_local_process.tui.first_level import FirstLevelPage
from fmri_local_process.tui.mriqc import MriqcPage
from fmri_local_process.tui.overview import OverviewPage
from fmri_local_process.tui.preprocess import PreprocessPage
from fmri_local_process.tui.scan_lengths import ScanLengthsPage
from fmri_local_process.tui.sequence import SequencePage
from fmri_local_process.tui.widgets import ConfirmScreen, Page, TextScreen

PAGES: dict[str, type[Page]] = {
    SETUP: ConvertPage,
    SESSIONS: SessionsPage,
    SEQUENCE: SequencePage,
    BEHAV: BehavPage,
    SCAN_LENGTHS: ScanLengthsPage,
    CROSSCHECK: CrosscheckPage,
    MRIQC: MriqcPage,
    PREPROCESS: PreprocessPage,
    OVERVIEW: OverviewPage,
    FIRST_LEVEL: FirstLevelPage,
}
STOP, LEAVE, CANCEL = "Stop C-PAC and quit", "Leave running and quit", "Cancel"


def pane_id(name: str) -> str:
    return f"tab-{TAB_ORDER.index(name) + 1}"


class ToolboxApp(App[None]):
    TITLE = "Philani fMRI toolbox"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #header { height: auto; padding: 0 1; background: $panel; }
    #header Static { width: auto; margin-right: 2; }
    #bids-name { text-style: bold; }
    #busy { color: $warning; text-style: bold; }
    .intro { border-left: thick $primary; padding: 0 1; text-style: bold; height: auto; }
    .muted { color: $text-muted; }
    Screen { overflow: hidden; }
    #tabs { height: 1fr; }
    #tabs > ContentSwitcher { height: 1fr; }
    #tabs TabPane { height: 1fr; padding: 0 1; }
    Page { height: 1fr; }
    .row { height: auto; overflow-x: auto; }
    .row Button { margin-right: 1; }
    .row Input { width: 20; }
    .row Input.wide { width: 1fr; }
    .row Select { width: 32; }
    .label { width: auto; padding: 1 1 0 0; }
    .section { text-style: bold; margin-top: 1; height: auto; }
    ModalScreen { align: center middle; }
    .dialog { width: 90%; max-width: 110; height: auto; max-height: 90%;
              border: thick $primary; background: $surface; padding: 1 2; }
    .dialog-title { text-style: bold; margin-bottom: 1; height: auto; }
    .dialog-body { height: auto; max-height: 24; }
    .dialog .buttons { height: auto; margin-top: 1; }
    .dialog .buttons Button { margin-right: 1; }
    .dialog OptionList, .dialog DirectoryTree { height: 16; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("left_square_bracket", "step_tab(-1)", "Previous tab"),
        Binding("right_square_bracket", "step_tab(1)", "Next tab"),
        Binding("question_mark", "help", "Help"),
        *(Binding(str(n % 10), f"open_tab({n})", show=False) for n in range(1, 11)),
    ]

    def __init__(self, make_settings: MakeSettings = json_settings, start_tab: str = SETUP) -> None:
        super().__init__()
        self.jobs: set[threading.Thread] = set()  # running now
        self.view_only = view_only()
        self.state = ToolboxState(make_settings)
        if self.view_only:  # locks every tab's changing actions, for good
            self.state.set_busy(VIEW_ONLY, True)
        self.behav_shared = BehavShared(self.state)  # one read of the logs for two tabs
        self.tab_names = tab_names(self.view_only)
        self.start_tab = start_tab if start_tab in self.tab_names else self.tab_names[0]
        self.pages: dict[str, Page] = {name: PAGES[name](self.state) for name in self.tab_names}

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static(id="bids-name", markup=False)
            yield Static(id="bids-path", classes="muted", markup=False)
            yield Static(id="busy", markup=False)
        with TabbedContent(initial=pane_id(self.start_tab), id="tabs"):
            for number, name in enumerate(self.tab_names, start=1):
                with TabPane(tab_text(number, name), id=pane_id(name)):
                    yield Static(TAB_INTROS[name], classes="intro", markup=False)
                    yield self.pages[name]
        yield Footer()

    def on_mount(self) -> None:
        self.title = title_text(self.view_only)
        self.state.folder_changed.connect(lambda _key: self.on_state_changed())
        self.state.busy_changed.connect(self.on_state_changed)
        self.state.data_changed.connect(self.show_current)
        self.on_state_changed()
        self.show_current()
        behav = self.pages.get(BEHAV)
        if isinstance(behav, BehavPage):  # ready by the time you get to it
            behav.show_page()

    # ---- tabs ---------------------------------------------------------------------------------

    @property
    def tabs(self) -> TabbedContent:
        return self.query_one("#tabs", TabbedContent)

    def current_page(self) -> Page | None:
        active = self.tabs.active
        name = next((n for n in self.tab_names if pane_id(n) == active), None)
        return self.pages[name] if name else None

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tabbed_content is self.tabs:  # not a tab's own tabs
            self.show_current()

    def show_current(self) -> None:
        page = self.current_page()
        if page is not None and page.is_mounted:
            page.show_page()

    def on_state_changed(self) -> None:
        for widget_id, text in zip(
            ("bids-name", "bids-path", "busy"), header_texts(self.state), strict=True
        ):
            self.query_one(f"#{widget_id}", Static).update(text)

    def set_badge(self, name: str, badge: str) -> None:
        if name in self.tab_names:
            tab = self.tabs.get_tab(pane_id(name))
            tab.label = tab_text(self.tab_names.index(name) + 1, name, badge)

    def action_step_tab(self, step: int) -> None:
        names = [pane_id(n) for n in self.tab_names]
        self.tabs.active = names[(names.index(self.tabs.active) + step) % len(names)]

    def action_open_tab(self, number: int) -> None:
        if number <= len(self.tab_names):
            self.tabs.active = pane_id(self.tab_names[number - 1])

    def action_help(self) -> None:
        page = self.current_page()
        if page is not None:
            keys = (
                "\n\nKeys: 1-9, 0 or [ ] change tab; Tab / Shift+Tab move; space ticks a row; "
                "Enter presses a button or keeps what you typed; Ctrl+Q quits."
            )
            self.push_screen(TextScreen("Help", (page.help_text() + keys).strip()))

    # ---- closing ------------------------------------------------------------------------------

    async def action_quit(self) -> None:
        if isinstance(self.screen, ConfirmScreen):
            return
        preprocess, mriqc = self.pages.get(PREPROCESS), self.pages.get(MRIQC)
        if CONVERTING in self.state.busy or TRIMMING in self.state.busy:
            text = (
                f"{self.state.lock_reason(PREPROCESSING)}. Quitting now stops it: the scan "
                "it is on is left out of the BIDS folder and done again next time. Quit anyway?"
            )
            self.push_screen(ConfirmScreen("Still working", text), self.quit_if_yes)
        elif isinstance(mriqc, MriqcPage) and mriqc.job is not None:
            job = mriqc.job

            def stop_mriqc(answer: str | None) -> None:
                if answer == "Yes":
                    job.stop_requested = True
                    self.leave_after(job.stop_and_wait)

            text = "Stop MRIQC and quit? The subject it is on is lost and runs again next time."
            self.push_screen(ConfirmScreen("MRIQC is still running", text), stop_mriqc)
        elif isinstance(preprocess, PreprocessPage) and preprocess.job is not None:
            self.push_screen(
                ConfirmScreen(
                    "C-PAC is still running",
                    "Stop C-PAC and quit, freeing the CPUs and memory? Finished steps are kept: "
                    "running the batch again later resumes each subject where it stopped.\n\n"
                    "Or quit and leave the running container(s) going in Docker (the rest of the "
                    "batch won't start).",
                    (STOP, LEAVE, CANCEL),
                ),
                lambda answer: self.quit_preprocess(preprocess, answer),
            )
        else:
            self.exit()

    def quit_if_yes(self, answer: str | None) -> None:
        if answer == "Yes":
            self.exit()

    def quit_preprocess(self, page: PreprocessPage, answer: str | None) -> None:
        if answer == LEAVE:
            self.exit()
        elif answer == STOP:
            page.activity.say("Stopping C-PAC before quitting (up to ~10 s)...")
            self.leave_after(page.stop_for_quit)

    def leave_after(self, work) -> None:  # noqa: ANN001 -- any no-argument callable
        """Stop the containers off the UI thread (docker takes ~10 s), then quit."""

        def body() -> None:
            work()
            self.call_from_thread(self.exit)

        threading.Thread(target=body, daemon=True).start()


@click.command(
    epilog="""
Over SSH, run it inside tmux: closing the connection then leaves it (and its jobs) running.
"""
)
@click.version_option(package_name="fmri-local-process")
def main() -> None:
    """The Philani fMRI toolbox in a terminal: the window's tabs, without a display."""
    if not sys.stdout.isatty():
        raise click.ClickException("fmri_tui needs a terminal (e.g. ssh -t <server> fmri_tui).")
    images.detect()
    app = ToolboxApp()
    app.run()
    if app.jobs:  # a job's thread may be blocked on docker for hours: leave at once
        shutil.rmtree(figures_dir(), ignore_errors=True)
        sys.stdout.flush()
        os._exit(app.return_code or 0)
    sys.exit(app.return_code or 0)


if __name__ == "__main__":
    main()
