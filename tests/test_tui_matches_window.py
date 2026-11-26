"""The same corrections, made in the terminal app and in the window on the same dummy data, must
save the same files and leave the same matches."""

from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("textual")

from PySide6.QtWidgets import QApplication  # noqa: E402
from test_tui import close_text, no_docker, page_of, press, run_app, settle, submit  # noqa: E402
from textual.pilot import Pilot  # noqa: E402
from textual.widgets import Input  # noqa: E402

from fmri_local_process.app.behav import load_behav_data  # noqa: E402
from fmri_local_process.app.tabs import BEHAV, SESSIONS  # noqa: E402
from fmri_local_process.gui.behav_logs_dialog import BehavLogsPanel  # noqa: E402
from fmri_local_process.processing.dummy_data import generate_dummy_dataset  # noqa: E402
from fmri_local_process.tui.behav import BehavPage, SessionsPage  # noqa: E402
from fmri_local_process.tui.toolbox import ToolboxApp  # noqa: E402
from fmri_local_process.tui.widgets import TickTable  # noqa: E402

__all__ = ["no_docker"]  # the fixture, used here too

FOLDER = "visit one DUMMY015"  # a DICOM folder with no date or ID in its name
FOLDER_FIX = ("DUMMY015", "2002-06-24", "1")


def log_key(behav: Path, text: str) -> str:
    logs = (behav / "emolabeling").rglob("*.log")
    return next(k for k in sorted(p.relative_to(behav).as_posix() for p in logs) if text in k)


def saved_files(bids: Path) -> dict[str, str]:
    return {
        p.relative_to(bids).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted((bids / "code").rglob("*"))
        if p.is_file() and "correction" in p.name
    }


def in_the_terminal(tmp: Path, bids: Path, behav: Path, dicom: Path, typo: str, bad: str) -> str:
    summary: list[str] = []

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        async def highlight(table_id: str, key: str) -> None:
            page = app.current_page()
            assert page is not None
            table = page.query_one(table_id, TickTable)
            table.move_cursor(row=table.row_keys.index(key))
            await settle(app, pilot)

        await highlight("#logs", typo)
        await submit(app, pilot, "#log-id", "DUMMY003")
        await highlight("#logs", bad)
        page_of(app, BehavPage).query_one("#note", Input).value = "stopped early"
        await settle(app, pilot)
        await press(app, pilot, "#error")
        app.query_one("#logs", TickTable).focus()
        await pilot.press(str(app.tab_names.index(SESSIONS) + 1))
        await settle(app, pilot)
        await highlight("#folders", FOLDER)
        page = page_of(app, SessionsPage)
        for box, value in zip(SessionsPage.BOXES, FOLDER_FIX, strict=True):
            page.query_one(box, Input).value = value
        await settle(app, pilot)
        await press(app, pilot, "#use")
        await press(app, pilot, "#save")
        await close_text(app, pilot)
        assert page.work is not None
        summary.append(page.work.summary())

    run_app(tmp, scenario, BEHAV, bids=bids, dicom=dicom, behav=behav)
    return summary[0]


def in_the_window(bids: Path, behav: Path, dicom: Path, typo: str, bad: str) -> str:
    _app = QApplication.instance() or QApplication([])
    panel = BehavLogsPanel(bids, behav, dicom, None, load_behav_data(bids, behav, dicom))
    keys = [info.key for info in panel.infos]
    panel.id_edits[keys.index(typo)].setText("DUMMY003")
    panel.recheck_button.click()
    shown = [m.info.key for m in panel.matches]
    panel.table.selectRow(shown.index(bad))
    panel.note_edit.setText("stopped early")
    panel.error_button.click()
    for edits, value in zip(
        (panel.session_edits, panel.folder_date_edits, panel.folder_session_edits),
        FOLDER_FIX,
        strict=True,
    ):
        edits[FOLDER].setText(value)
    panel.sessions_recheck_button.click()
    panel.save_button.click()
    return panel.work.summary()


def test_the_same_corrections_save_the_same(tmp_path: Path) -> None:
    generate_dummy_dataset(tmp_path / "dummy", 1, with_errors=True, seed=1)
    behav, dicom = tmp_path / "dummy" / "behav", tmp_path / "dummy" / "dicom"
    typo, bad = log_key(behav, "DUMMY0033"), log_key(behav, "DUMMY000")
    terminal, window = tmp_path / "terminal", tmp_path / "window"
    for bids in (terminal, window):
        bids.mkdir()
    found = in_the_terminal(tmp_path, terminal, behav, dicom, typo, bad)
    assert found == in_the_window(window, behav, dicom, typo, bad)
    files = saved_files(terminal)
    assert files and files == saved_files(window)
    assert "stopped early" in "".join(files.values())
