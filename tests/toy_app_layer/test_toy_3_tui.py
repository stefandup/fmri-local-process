"""Step 3: the terminal app on the same app layer. Textual's run_test() runs the app without
a terminal, and `pilot` clicks and types like a user would. The same two tests as step 2."""

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("textual")  # until textual is in the requirements

from test_toy_1_app import make_folder  # noqa: E402
from textual.widgets import DataTable, Input, Static, TabbedContent  # noqa: E402
from toy_app import JsonSettings, ToolboxState  # noqa: E402
from toy_tui import ToyTui  # noqa: E402


def make_app(tmp_path: Path) -> ToyTui:
    # The TUI hands the state a JSON file instead of a QSettings.
    return ToyTui(ToolboxState(JsonSettings(tmp_path / "settings.json")))


def header(app: ToyTui) -> str:
    return str(app.query_one("#header", Static).content)


def test_choosing_a_folder_updates_the_header(tmp_path: Path) -> None:
    folder = make_folder(tmp_path)
    app = make_app(tmp_path)

    async def scenario() -> None:  # Textual is async; asyncio.run() below drives it
        async with app.run_test() as pilot:
            app.query_one("#folder-input", Input).value = str(folder)
            await pilot.click("#use-folder")  # -> state.set_folder -> update_header
            assert header(app) == "notes"

    asyncio.run(scenario())


def test_counting_runs_on_a_thread_and_fills_the_table(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    app.state.set_folder(make_folder(tmp_path))

    async def scenario() -> None:
        async with app.run_test() as pilot:
            app.query_one(TabbedContent).active = "tab-2"  # switch to "2. Line counts"
            await pilot.pause()
            await pilot.click("#run")
            await app.workers.wait_for_complete()  # the worker thread is done...
            await pilot.pause()  # ...and its call_from_thread updates have run

            assert app.query_one("#counts", DataTable).row_count == 2
            assert str(app.query_one("#status", Static).content) == "2 files, 5 lines"
            assert header(app) == "notes"

    asyncio.run(scenario())
