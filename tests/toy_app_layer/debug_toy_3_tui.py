"""Debug harness for step 3: the Textual terminal app (toy_tui.py) on the same app layer.

Not a test and not part of the app. A full-screen terminal app and a debugger fight over the
terminal, so this runs the app headless with Textual's run_test(): no screen, and a `pilot`
that types and clicks for you. After each step it saves a screenshot (an .svg you can open in
a browser) to the work folder, so you can still see what the screen looked like.

Run it from Run and Debug -> "Debug toy step 3". Without the debugger it just prints.

Textual is async: the steps live in an `async def`, and asyncio.run() drives it. Every
`await` is a point where Textual gets to handle what is waiting (the role refresh_screen()
plays in the Qt harnesses).
"""

import asyncio
import tempfile
from pathlib import Path

from textual.widgets import DataTable, Input, Static, TabbedContent
from toy_app import JsonSettings, ToolboxState
from toy_data import make_folder
from toy_tui import ToyTui

# ---- Inputs ---------------------------------------------------------------------------------

# Where this harness keeps its files: <your temp folder>\toy_debug\step3 (not the repo).
WORK_FOLDER: Path = Path(tempfile.gettempdir()) / "toy_debug" / "step3"

# The JSON settings file the state reads and writes (the TUI has no registry).
SETTINGS_FILE: Path = WORK_FOLDER / "settings.json"


def header_of(app: ToyTui) -> str:
    header_widget = app.query_one("#header", Static)
    return str(header_widget.content)


def save_screen(app: ToyTui, step_name: str) -> Path:
    """Save what the terminal would show now as WORK_FOLDER/<step_name>.svg."""
    file_name = f"{step_name}.svg"
    app.save_screenshot(filename=file_name, path=str(WORK_FOLDER))
    return WORK_FOLDER / file_name


async def drive_the_app(app: ToyTui, notes_folder: Path) -> None:
    async with app.run_test() as pilot:  # starts the app headless; ends it at the block's end
        # ---- 1. Just started ----------------------------------------------------------------
        header_at_start: str = header_of(app)
        screen_at_start: Path = save_screen(app, "1_start")

        # ---- 2. Choose the folder on tab 1 --------------------------------------------------
        folder_input = app.query_one("#folder-input", Input)
        folder_input.value = str(notes_folder)  # what the user would type
        await pilot.click("#use-folder")  # Textual calls FolderPane.on_button_pressed
        header_after_click: str = header_of(app)
        settings_file_text: str = SETTINGS_FILE.read_text()
        screen_after_click: Path = save_screen(app, "2_folder_chosen")

        # ---- 3. Switch to tab 2 and count ---------------------------------------------------
        app.query_one(TabbedContent).active = "tab-2"  # show "2. Line counts"
        await pilot.pause()  # let Textual redraw
        await pilot.click("#run")  # CountPane.start() -> run_worker(job.run, thread=True)
        busy_right_after_start: set[str] = set(app.state.busy)

        await app.workers.wait_for_complete()  # wait until the worker thread is done
        await pilot.pause()  # let the call_from_thread updates run
        counts_table = app.query_one("#counts", DataTable)
        rows_at_end: int = counts_table.row_count
        status_at_end: str = str(app.query_one("#status", Static).content)
        header_at_end: str = header_of(app)
        screen_at_end: Path = save_screen(app, "3_counted")

    # ---- What happened ----------------------------------------------------------------------
    print(f"1 header:               {header_at_start!r}")
    print(f"2 header:               {header_after_click!r}")
    print(f"  settings file:        {settings_file_text}")
    print(f"3 busy right after run: {busy_right_after_start}")
    print(
        f"  at end:               rows={rows_at_end}  status={status_at_end!r}  "
        f"header={header_at_end!r}"
    )
    print(
        f"screenshots:            {screen_at_start}\n"
        f"                        {screen_after_click}\n"
        f"                        {screen_at_end}"
    )


def main() -> None:
    SETTINGS_FILE.unlink(missing_ok=True)  # start like a first launch
    notes_folder: Path = make_folder(WORK_FOLDER)  # a.txt, b.txt, skip.csv (see toy_data.py)
    state = ToolboxState(JsonSettings(SETTINGS_FILE))
    app = ToyTui(state)
    asyncio.run(drive_the_app(app, notes_folder))


if __name__ == "__main__":
    main()
