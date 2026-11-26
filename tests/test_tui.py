"""The terminal app, driven through Textual's test pilot on synthetic folders: no display, no
Docker, no Qt."""

import asyncio
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text  # noqa: E402
from test_behav_match import emo_log  # noqa: E402
from test_crosscheck import func, write_bold, write_nifti  # noqa: E402
from test_first_level import write_run as write_cpac_run  # noqa: E402
from test_mriqc import make_bids as make_mriqc_bids  # noqa: E402
from test_mriqc import write_iqms  # noqa: E402
from textual.pilot import Pilot  # noqa: E402
from textual.widgets import Button, Checkbox, Input, Static  # noqa: E402

from fmri_local_process.app import backup, image_download, preprocess  # noqa: E402
from fmri_local_process.app import mriqc as app_mriqc  # noqa: E402
from fmri_local_process.app.settings import JsonSettings  # noqa: E402
from fmri_local_process.app.state import CONVERTING, ToolboxState  # noqa: E402
from fmri_local_process.app.tabs import (  # noqa: E402
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
    TAB_ORDER,
    tab_text,
)
from fmri_local_process.processing import atlases as at  # noqa: E402
from fmri_local_process.processing import crosscheck as cc  # noqa: E402
from fmri_local_process.processing import mriqc as mq  # noqa: E402
from fmri_local_process.processing import philani_bids as pb  # noqa: E402
from fmri_local_process.processing import review_mode  # noqa: E402
from fmri_local_process.processing.action_log import read_actions  # noqa: E402
from fmri_local_process.processing.philani_bids import ConversionReport  # noqa: E402
from fmri_local_process.tui import convert as tui_convert  # noqa: E402
from fmri_local_process.tui import images  # noqa: E402
from fmri_local_process.tui.behav import BehavPage, SessionsPage  # noqa: E402
from fmri_local_process.tui.convert import ConvertPage  # noqa: E402
from fmri_local_process.tui.crosscheck import CrosscheckPage  # noqa: E402
from fmri_local_process.tui.first_level import FirstLevelPage  # noqa: E402
from fmri_local_process.tui.mriqc import MriqcPage  # noqa: E402
from fmri_local_process.tui.overview import OverviewPage  # noqa: E402
from fmri_local_process.tui.preprocess import PreprocessPage  # noqa: E402
from fmri_local_process.tui.scan_lengths import ScanLengthsPage  # noqa: E402
from fmri_local_process.tui.sequence import SequencePage  # noqa: E402
from fmri_local_process.tui.toolbox import PAGES, ToolboxApp, pane_id  # noqa: E402
from fmri_local_process.tui.widgets import (  # noqa: E402
    ConfirmScreen,
    DetailBox,
    FilledSelect,
    FolderBox,
    ImageBox,
    InputScreen,
    Line,
    Notices,
    PickScreen,
    Table,
    TextScreen,
    TickTable,
)

Scenario = Callable[[ToolboxApp, Pilot], Awaitable[None]]


@pytest.fixture(autouse=True)
def no_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never ask the real Docker, and keep the user's own folders out of it."""
    monkeypatch.setattr(tui_convert, "check_defacer", lambda: ("no defacer (test)", ""))
    probe = preprocess.DockerProbe(64.0, 16, "", True)
    monkeypatch.setattr(preprocess, "probe_docker", lambda: probe)
    monkeypatch.setattr(app_mriqc, "probe_docker", lambda: (mq.READY, "", 8, 32.0))
    monkeypatch.setattr(backup, "backup_base", lambda: tmp_path / "backups")
    monkeypatch.setattr(at, "default_cache", lambda: tmp_path / "atlases")
    monkeypatch.setattr(images, "_inline", None)


def make_settings(tmp_path: Path) -> Callable[[str], JsonSettings]:
    return lambda app: JsonSettings(tmp_path / "settings" / f"{app}.json")


def run_app(tmp_path: Path, scenario: Scenario, tab: str = SETUP, /, **folders: Path) -> None:
    """The app with these folders chosen, opened on a tab, while scenario drives it."""
    run_sized(tmp_path, scenario, tab, (140, 60), folders)


def run_sized(
    tmp_path: Path, scenario: Scenario, tab: str, size: tuple[int, int], folders: dict[str, Path]
) -> None:
    state = ToolboxState(make_settings(tmp_path))
    for key, folder in folders.items():
        folder.mkdir(parents=True, exist_ok=True)
        state.set_folder(key, folder)

    async def main() -> None:
        app = ToolboxApp(make_settings(tmp_path), tab)
        async with app.run_test(size=size) as pilot:
            await settle(app, pilot)
            await scenario(app, pilot)

    asyncio.run(main())


async def settle(app: ToolboxApp, pilot: Pilot) -> None:
    """Until the background jobs are done and the screen has caught up."""
    await pilot.pause()
    for _ in range(500):
        if not app.jobs:
            break
        await pilot.pause(0.02)
    assert not app.jobs
    await pilot.pause()
    await pilot.pause()


async def press(app: ToolboxApp, pilot: Pilot, selector: str) -> None:
    """A button (or tick) on the open page, wherever it has been scrolled to."""
    page = app.current_page()
    assert page is not None
    button = page.query_one(selector, Button)
    assert not button.disabled, f"{selector} is greyed out"
    button.press()
    await settle(app, pilot)


async def answer(app: ToolboxApp, pilot: Pilot, text: str = "Yes") -> None:
    assert isinstance(app.screen, ConfirmScreen)
    next(b for b in app.screen.query(Button) if str(b.label) == text).press()
    await settle(app, pilot)


async def close_text(app: ToolboxApp, pilot: Pilot) -> str:
    screen = app.screen
    assert isinstance(screen, TextScreen)
    await pilot.press("escape")
    await settle(app, pilot)
    return screen.text


async def submit(app: ToolboxApp, pilot: Pilot, selector: str, text: str) -> None:
    page = app.current_page()
    assert page is not None
    box = page.query_one(selector, Input)
    box.focus()
    box.value = text
    await pilot.press("enter")
    await settle(app, pilot)


def page_of[P](app: ToolboxApp, kind: type[P]) -> P:
    page = app.current_page()
    assert isinstance(page, kind)
    return page


def line(page: object, selector: str) -> str:
    return page.query_one(selector, Line).text  # pyright: ignore[reportAttributeAccessIssue]


def two_sessions(bids: Path) -> None:
    """sub-A with ses-1 and ses-2, a Rest run each (ses-2's is longer)."""
    for session, volumes in (("1", 10), ("2", 14)):
        name = "task-Rest_bold.nii.gz"
        write_bold(func(bids, "A", session, name), volumes)
    (bids / "sub-A" / "sub-A_sessions.tsv").write_text(
        "session_id\tacq_time\tsource_folder\n"
        "ses-1\t2024-10-14T13:41:00\t20241014_1341_A\n"
        "ses-2\t2024-10-15T09:00:00\t20241015_0900_A\n"
    )


# ---- the app ------------------------------------------------------------------------------------


def test_the_terminal_app_never_loads_qt() -> None:
    code = (
        "import sys; sys.modules['PySide6'] = None\n"
        "import fmri_local_process.tui.toolbox\n"
        "assert not [m for m in sys.modules if m.startswith('PySide6.')]\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_tab_opens_in_the_order_of_the_work_on_nothing(tmp_path: Path) -> None:
    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        labels = [str(app.tabs.get_tab(pane_id(n)).label) for n in app.tab_names]
        assert labels == [tab_text(i, n) for i, n in enumerate(TAB_ORDER, start=1)]
        for number, name in enumerate(TAB_ORDER, start=1):
            await pilot.press(str(number % 10))
            await settle(app, pilot)
            assert type(app.current_page()) is PAGES[name]
        await pilot.press("question_mark")
        assert isinstance(app.screen, TextScreen) and "<b>" not in app.screen.text
        await close_text(app, pilot)
        await pilot.press("right_square_bracket")  # round to the first tab
        await settle(app, pilot)
        assert isinstance(app.current_page(), ConvertPage)

    run_app(tmp_path, scenario)


@pytest.mark.parametrize("size", [(80, 24), (140, 60)])
def test_every_button_can_be_reached_in_a_small_terminal(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    """Nothing may lie to the right of the screen without a way to scroll to it."""
    bids = tmp_path / "bids"
    two_sessions(bids)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        for name in app.tab_names:
            app.tabs.active = pane_id(name)
            await settle(app, pilot)
            page = app.current_page()
            assert page is not None
            for widget in (*page.query(Button), *page.query(Input)):
                if not widget.display or widget.disabled or not widget.region.width:
                    continue
                widget.scroll_visible(animate=False, immediate=True)
                await pilot.pause()
                assert widget.region.right <= size[0], f"{name}: {widget} is cut off"

    run_sized(tmp_path, scenario, SETUP, size, {"bids": bids})


def test_the_keys_leave_every_tab_whatever_is_focused(tmp_path: Path) -> None:
    """Hiding a tab moves the focus; it must not bring that tab back."""
    folders, _folder, _log = behav_study(tmp_path)
    stuck: list[str] = []

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        for number, name in enumerate(app.tab_names, start=1):
            for kind in (Button, Table, Checkbox):
                app.action_open_tab(number)
                await settle(app, pilot)
                widget = next((w for w in app.pages[name].query(kind) if w.focusable), None)
                if widget is None:
                    continue
                widget.focus()
                await pilot.pause()
                await pilot.press(
                    "]" if kind is Table else str(number % len(app.tab_names) + 1)[-1]
                )
                await settle(app, pilot)
                if app.tabs.active == pane_id(name):
                    stuck.append(f"{name}: from {widget}")

    run_app(tmp_path, scenario, **folders)
    assert not stuck


def test_review_package_is_view_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "package"
    (package / "review_data" / "bids").mkdir(parents=True)
    (package / review_mode.MANIFEST).write_text(
        '{"name": "demo", "view_only": true, "folders": {"bids": "review_data/bids"}}'
    )
    monkeypatch.setenv(review_mode.ENV_VAR, str(package))
    two_sessions(package / "review_data" / "bids")

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        assert SETUP not in app.tab_names and PREPROCESS not in app.tab_names
        assert BEHAV not in app.tab_names  # no logs in the package
        app.tabs.active = pane_id(CROSSCHECK)
        await settle(app, pilot)
        page = page_of(app, CrosscheckPage)
        assert page.query_one("#delete", Button).disabled
        assert not page.query_one("#mark", Button).disabled  # marks and notes still work

    run_app(tmp_path, scenario)


def test_quitting_asks_while_converting(tmp_path: Path) -> None:
    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        app.state.set_busy(CONVERTING, True)
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "Quit anyway?" in app.screen.text
        await pilot.press("enter")  # the safe answer has the focus
        await pilot.pause()
        assert app.is_running and not isinstance(app.screen, ConfirmScreen)
        await pilot.press("ctrl+q", "n")
        await pilot.pause()
        assert app.is_running and not isinstance(app.screen, ConfirmScreen)
        app.state.set_busy(CONVERTING, False)
        assert app.query_one("#busy", Static).content == ""

    run_app(tmp_path, scenario)


# ---- Setup --------------------------------------------------------------------------------------


def test_folders_are_typed_checked_made_and_remembered(tmp_path: Path) -> None:
    raw = tmp_path / "raw [x] ^"  # neither markup nor special
    raw.mkdir()

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, ConvertPage)
        assert any("No DICOM sessions folder" in m for m in page.query_one(Notices).shown)
        await submit(app, pilot, "#folder-dicom Input", str(raw))
        assert app.state.folder("dicom") == raw
        await submit(app, pilot, "#folder-bids Input", str(raw / "bids"))  # inside the raw data
        assert "its own output" in await close_text(app, pilot)
        assert app.state.folder("bids") is None
        await submit(app, pilot, "#folder-bids Input", str(tmp_path / "bids"))
        await answer(app, pilot)  # make it
        assert app.state.folder("bids") == tmp_path / "bids" and (tmp_path / "bids").is_dir()
        assert app.query_one("#bids-name", Static).content == "bids"
        await submit(app, pilot, "#folder-behav Input", str(tmp_path / "typo"))
        assert "no folder" in await close_text(app, pilot)  # raw folders are never made

    run_app(tmp_path, scenario)
    state = ToolboxState(make_settings(tmp_path))
    assert state.folder("dicom") == raw and state.folder("bids") == tmp_path / "bids"


def test_convert_shows_progress_and_report_then_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    go = threading.Event()

    def fake_run(job: tui_convert.ConvertJob) -> ConversionReport:
        job.progress(1, 2, "sub-01_ses-1")
        assert go.wait(10)
        report = ConversionReport()
        report.failures.append(("sub-01", "dcm2niix stopped"))
        return report

    monkeypatch.setattr(tui_convert.ConvertJob, "run", fake_run)
    raw, bids = tmp_path / "raw", tmp_path / "bids"
    (raw / "20240101_1200_01").mkdir(parents=True)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, ConvertPage)
        assert "No defacer" in line(page, "#defacer") and page.query_one("#deface").disabled
        page.query_one("#convert", Button).press()
        for _ in range(100):
            await pilot.pause(0.02)
            if "sub-01_ses-1" in line(page, "#convert-now"):
                break
        assert CONVERTING in app.state.busy
        assert page.query_one("#folder-bids Input", Input).disabled  # locked meanwhile
        go.set()
        await settle(app, pilot)
        assert CONVERTING not in app.state.busy and page.job is None
        assert page.query_one("#summary", Table).texts("failed") == ["failed", "1"]
        assert "FAILED  sub-01: dcm2niix stopped" in page.activity.said

    run_app(tmp_path, scenario, dicom=raw, bids=bids)


def test_a_crashing_job_is_shown_and_unlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(_job: tui_convert.ConvertJob) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(tui_convert.ConvertJob, "run", crash)
    raw, bids = tmp_path / "raw", tmp_path / "bids"
    (raw / "20240101_1200_01").mkdir(parents=True)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        await press(app, pilot, "#convert")
        assert "RuntimeError: boom" in await close_text(app, pilot)
        assert not app.state.busy and app.is_running

    run_app(tmp_path, scenario, dicom=raw, bids=bids)


def test_sessions_per_subject_and_backup(tmp_path: Path) -> None:
    bids = tmp_path / "bids"

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, ConvertPage)
        await submit(app, pilot, "#sessions", "x")
        assert "whole number" in await close_text(app, pilot)
        await submit(app, pilot, "#sessions", "3")
        assert cc.read_expected_sessions(bids) == 3
        await press(app, pilot, "#backup-save")
        assert (tmp_path / "backups" / "philani").is_dir()
        assert "Saved" in line(page, "#backup-status")
        await press(app, pilot, "#backup-restore")
        assert "Folders needed" in app.screen.title_text  # pyright: ignore[reportAttributeAccessIssue]
        await close_text(app, pilot)

    run_app(tmp_path, scenario, bids=bids)


def test_restore_asks_then_converts_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tui_convert.ConvertJob, "run", lambda _job: ConversionReport())
    raw, bids = tmp_path / "raw", tmp_path / "bids"
    (raw / "20240101_1200_01").mkdir(parents=True)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        await submit(app, pilot, "#sessions", "2")
        await press(app, pilot, "#backup-save")
        (bids / cc.SETTINGS_FILE).unlink()  # lost
        await press(app, pilot, "#backup-restore")
        assert isinstance(app.screen, PickScreen)
        await pilot.press("enter")
        await settle(app, pilot)
        await answer(app, pilot)
        assert "Restore finished" in await close_text(app, pilot)
        assert cc.read_expected_sessions(bids) == 2

    run_app(tmp_path, scenario, dicom=raw, bids=bids)


def test_scan_rules_are_checked_and_saved(tmp_path: Path) -> None:
    raw, bids = tmp_path / "raw", tmp_path / "bids"
    (raw / "20240101_1200_01" / "3_t1_mprage").mkdir(parents=True)
    (raw / "20240101_1200_01" / "3_t1_mprage" / "a.dcm").write_bytes(b"x")

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        await press(app, pilot, "#rules")
        screen = app.screen
        screen.query_one("#pattern", Input).value = "*t1_mprage*"
        screen.query_one("#scan", Input).value = "nonsense"
        screen.query_one("#add", Button).press()
        await pilot.pause()
        assert line(screen, "#rule-problem")
        screen.query_one("#scan", Input).value = "T1w"
        screen.query_one("#add", Button).press()
        await pilot.pause()
        screen.query_one("#check", Button).press()
        await pilot.pause()
        assert "T1w <- 3_t1_mprage" in line(screen, "#report")
        screen.query_one("#save", Button).press()
        await settle(app, pilot)
        assert any(r.pattern == "*t1_mprage*" for r in pb.read_scan_rules(bids))

    run_app(tmp_path, scenario, dicom=raw, bids=bids)


# ---- Raw DICOMs and Behaviour logs --------------------------------------------------------------


def behav_study(tmp_path: Path) -> tuple[dict[str, Path], str, str]:
    bids, raw, logs = tmp_path / "bids", tmp_path / "raw", tmp_path / "logs"
    folder = "20241014_1341_12345-1"
    series = raw / folder / "4_labeling_task_AP"
    series.mkdir(parents=True)
    (series / "img.dcm").write_bytes(b"fake")
    log = "emolabeling/12345-2-emolabeling_scan-20241014-151000-BBBBB.log"  # twin 2: a typo
    (logs / log).parent.mkdir(parents=True)
    (logs / log).write_text(emo_log(), encoding="utf-8")
    return {"bids": bids, "dicom": raw, "behav": logs}, folder, log


def test_a_wrong_log_id_is_corrected_marked_and_saved(tmp_path: Path) -> None:
    folders, _folder, log = behav_study(tmp_path)
    bids = folders["bids"]

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, BehavPage)
        assert "(1 to review)" in str(app.tabs.get_tab(pane_id(BEHAV)).label)
        assert page.query_one("#events", Table).row_count > 0
        await submit(app, pilot, "#log-date", "2024-02-30")
        assert "not a date" in line(page, "#problem")
        page.query_one("#log-date", Input).value = ""
        await submit(app, pilot, "#log-id", "12345-1")
        assert page.work is not None and page.work.matches[0].status == "matched"
        assert "not saved yet" in line(page, "#status")
        page.query_one("#logs", TickTable).focus()
        await pilot.press("space")
        await settle(app, pilot)
        await press(app, pilot, "#mark")
        page.query_one("#note", Input).value = "fine [really]"
        await press(app, pilot, "#ok")
        assert not (bids / "code").exists()  # only Save writes
        await press(app, pilot, "#save")
        assert "Corrections saved" in await close_text(app, pilot)
        assert "review" not in str(app.tabs.get_tab(pane_id(BEHAV)).label)

    run_app(tmp_path, scenario, BEHAV, **folders)
    from fmri_local_process.processing.behav_match import read_corrections

    saved = read_corrections(bids)[log]
    assert (saved.subject, saved.review, saved.note) == ("12345-1", "ok", "fine [really]")
    assert saved.crosschecked


def test_the_match_comes_first_and_the_box_shows_the_highlighted_row(tmp_path: Path) -> None:
    folders, folder, log = behav_study(tmp_path)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, BehavPage)
        table = page.query_one("#logs", TickTable)
        assert table.names[1:5] == ["Match", "Task", "ID in name", "Log state"]  # after the tick
        assert table.names[-1] == "Log"
        cells = [str(c) for c in table.get_row_at(0)]
        assert cells[1] == "unknown ID" and cells[-1] == Path(log).name  # no folder
        box = page.query_one("#details", DetailBox)
        assert box.display and box.value("Match") == "unknown ID"
        assert box.value("Log file date").startswith("2024-10-14")
        await submit(app, pilot, "#log-id", "12345-1")
        assert box.value("Corrected ID") == "12345-1"
        assert box.value("Match").startswith("matched -> ")

        table.focus()  # number keys switch tabs, but not while typing in a box
        await pilot.press(str(app.tab_names.index(SESSIONS) + 1))
        await settle(app, pilot)
        sessions = page_of(app, SessionsPage)
        folder_box = sessions.query_one("#details", DetailBox)
        assert folder_box.display and folder_box.value("Scanned") == "2024-10-14 13:41"
        await submit(app, pilot, "#folder-session", "2")
        assert folder_box.value("Set session") == "2"

    run_app(tmp_path, scenario, BEHAV, **folders)


def test_the_log_view_dropdown_filters_the_logs(tmp_path: Path) -> None:
    folders, _folder, _log = behav_study(tmp_path)  # one log, its ID wrong: unmatched

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, BehavPage)
        view = page.query_one("#log-view", FilledSelect)
        table = page.query_one("#logs", TickTable)
        assert view.chosen() == "All logs" and table.row_count == 1
        shown = {
            "Matched and needs review": 0,
            "Needs review only": 1,
            "Matched": 0,
            "Unmatched": 1,
        }
        for choice, rows in shown.items():
            view.value = choice
            await settle(app, pilot)
            assert table.row_count == rows, choice
        view.value = "All logs"
        await settle(app, pilot)
        await submit(app, pilot, "#log-id", "12345-1")  # now matched, nothing left to review
        for choice, rows in {"Matched": 1, "Unmatched": 0, "Needs review only": 0}.items():
            view.value = choice
            await settle(app, pilot)
            assert table.row_count == rows, choice

    run_app(tmp_path, scenario, BEHAV, **folders)


def test_refresh_cache_reads_every_log_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders, _folder, _log = behav_study(tmp_path)
    from fmri_local_process.processing import behav_match as bm

    read: list[Path] = []
    real = bm.inspect_log

    def counted(log_file: Path, *args: object, **kwargs: object) -> bm.LogInfo:
        read.append(log_file)
        return real(log_file, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bm, "inspect_log", counted)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, BehavPage)
        assert len(read) == 1 and page.query_one("#refresh", Button).tooltip
        await press(app, pilot, "#reload")
        assert len(read) == 1  # unchanged: from the cache
        await press(app, pilot, "#refresh")
        assert len(read) == 2
        assert page.query_one("#logs", TickTable).row_count == 1

    run_app(tmp_path, scenario, BEHAV, **folders)


def test_what_is_typed_for_a_log_is_never_lost(tmp_path: Path) -> None:
    folders, _folder, log = behav_study(tmp_path)
    other = log.replace("151000", "161000")
    (folders["behav"] / other).write_text(emo_log(), encoding="utf-8")

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, BehavPage)
        table = page.query_one("#logs", TickTable)
        box = page.query_one("#log-id", Input)
        assert table.current_key() == log
        box.value = "12345-1"
        await settle(app, pilot)
        assert "not saved yet" in line(page, "#status")
        table.move_cursor(row=1)  # another log, without pressing Use
        await settle(app, pilot)
        assert box.value == ""
        page.query_one("#log-date", Input).value = "2024-13-01"
        await settle(app, pilot)
        table.move_cursor(row=0)
        await settle(app, pilot)
        assert box.value == "12345-1"  # still there
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "aren't saved" in app.screen.text
        await answer(app, pilot, "No")
        await press(app, pilot, "#save")
        assert other in await close_text(app, pilot)  # names the log with the wrong date
        assert not (folders["bids"] / "code").exists()
        table.move_cursor(row=1)
        await settle(app, pilot)
        page.query_one("#log-date", Input).value = ""
        page.query_one("#note", Input).value = "restarted"
        await settle(app, pilot)
        table.move_cursor(row=0)
        await settle(app, pilot)
        await press(app, pilot, "#ok")  # uses what is typed for this log, too
        assert page.work is not None and page.work.matches[0].status == "matched"
        table.move_cursor(row=1)
        await settle(app, pilot)
        assert page.query_one("#note", Input).value == "restarted"  # waits for its verdict
        await press(app, pilot, "#error")
        await press(app, pilot, "#save")
        await close_text(app, pilot)
        assert not app.behav_shared.anything_unsaved()

    run_app(tmp_path, scenario, BEHAV, **folders)
    from fmri_local_process.processing.behav_match import read_corrections

    saved = read_corrections(folders["bids"])
    assert (saved[log].subject, saved[log].review) == ("12345-1", "ok")
    assert (saved[other].review, saved[other].note) == ("error", "restarted")


def test_corrections_typed_on_both_tabs_are_saved_together(tmp_path: Path) -> None:
    folders, folder, _log = behav_study(tmp_path)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page_of(app, SessionsPage).query_one("#folder-id", Input).value = "12345-2"
        await settle(app, pilot)
        app.tabs.active = pane_id(BEHAV)
        await settle(app, pilot)
        await press(app, pilot, "#save")
        await close_text(app, pilot)
        logs = page_of(app, BehavPage).work
        assert logs is not None and logs.matches[0].status == "matched"

    run_app(tmp_path, scenario, SESSIONS, **folders)
    assert pb.read_dicom_corrections(folders["bids"]) == {folder: "12345-2"}


def test_a_note_is_saved_when_its_box_is_left(tmp_path: Path) -> None:
    bids = tmp_path / "bids"
    two_sessions(bids)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, CrosscheckPage)
        note = page.query_one("#note", Input)
        note.focus()
        await pilot.press("o", "k")
        page.table.focus()
        await settle(app, pilot)
        assert [r.note for r in page.reviews] == ["ok", ""]

    run_app(tmp_path, scenario, CROSSCHECK, bids=bids)


def test_a_scan_folder_is_corrected_on_raw_dicoms(tmp_path: Path) -> None:
    folders, folder, _log = behav_study(tmp_path)
    bids = folders["bids"]

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, SessionsPage)
        table = page.query_one("#folders", TickTable)
        assert table.texts(folder)[3] == "12345-1"
        await submit(app, pilot, "#folder-session", "x")
        assert "not a session number" in line(page, "#problem")
        page.query_one("#folder-session", Input).value = "2"
        await submit(app, pilot, "#folder-id", "12345-2")
        assert "12345T2" in " ".join(table.texts(folder)) and "ses-2" in " ".join(
            table.texts(folder)
        )
        table.tick_all(True)
        await settle(app, pilot)
        await press(app, pilot, "#mark")
        await press(app, pilot, "#save")
        await close_text(app, pilot)

    run_app(tmp_path, scenario, SESSIONS, **folders)
    assert pb.read_dicom_corrections(bids) == {folder: "12345-2"}
    assert pb.read_dicom_session_corrections(bids) == {folder: "2"}
    assert pb.read_crosschecked_folders(bids) == {folder}


def test_logs_export_into_a_chosen_folder(tmp_path: Path) -> None:
    folders, _folder, _log = behav_study(tmp_path)
    out = tmp_path / "cpac"

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        await press(app, pilot, "#export")
        assert isinstance(app.screen, PickScreen)
        await pilot.press("down", "enter")  # C-PAC's output folder
        await settle(app, pilot)
        assert isinstance(app.screen, PickScreen)
        await pilot.press("enter")  # one folder
        await settle(app, pilot)
        assert "1 events file(s) written" in await close_text(app, pilot)

    run_app(tmp_path, scenario, BEHAV, output=out, **folders)
    assert len(list(out.rglob("*_events.tsv"))) == 1


# ---- Scan lengths -------------------------------------------------------------------------------


def test_expected_length_is_saved_then_long_runs_trimmed(tmp_path: Path) -> None:
    bids = tmp_path / "bids"
    two_sessions(bids)
    label = "task-Rest_bold"

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, ScanLengthsPage)
        table = page.query_one("#lengths", Table)
        assert table.texts(label)[0:3] == [label, "2", "10, 14"]
        await submit(app, pilot, "#expected", "ten")
        assert "whole number" in await close_text(app, pilot)
        await submit(app, pilot, "#expected", "10")
        assert cc.read_expected(bids) == {label: 10}
        assert table.texts(label)[7] == "1"  # too long
        await press(app, pilot, "#trim")
        assert "14 -> 10" in app.screen.text  # pyright: ignore[reportAttributeAccessIssue]
        await answer(app, pilot)
        assert any("to 10 volumes" in said for said in page.activity.said)
        assert table.texts(label)[7:9] == ["", "Trimmed 1 of 2"]
        await press(app, pilot, "#automatic")
        assert cc.read_expected(bids) == {}

    run_app(tmp_path, scenario, SCAN_LENGTHS, bids=bids)


# ---- Crosscheck and overview --------------------------------------------------------------------


def test_crosscheck_marks_notes_and_deletes(tmp_path: Path) -> None:
    bids = tmp_path / "bids"
    two_sessions(bids)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, CrosscheckPage)
        assert [r.name for r in page.reviews] == ["sub-A_ses-1", "sub-A_ses-2"]
        assert "to check" in str(app.tabs.get_tab(pane_id(CROSSCHECK)).label)
        await press(app, pilot, "#mark")
        assert page.reviews[0].crosschecked and not page.reviews[1].crosschecked
        await submit(app, pilot, "#note", "moved [a lot]")
        assert page.reviews[0].note == "moved [a lot]"
        page.table.focus()
        await pilot.press("down", "space")
        await settle(app, pilot)
        assert line(page, "#ticked") == "1 ticked"
        assert line(page, "#title").startswith("sub-A_ses-2")  # the highlighted one
        await press(app, pilot, "#note-ticked")
        assert isinstance(app.screen, InputScreen)
        app.screen.query_one(Input).value = "dud"
        await pilot.press("enter")
        await settle(app, pilot)
        await press(app, pilot, "#delete-ticked")
        await answer(app, pilot, "No")
        assert len(page.reviews) == 2
        await press(app, pilot, "#delete-ticked")
        await answer(app, pilot)
        assert [r.name for r in page.reviews] == ["sub-A_ses-1"]
        assert not (bids / "sub-A" / "ses-2").exists()

    run_app(tmp_path, scenario, CROSSCHECK, bids=bids)
    assert pb.read_excluded_sessions(bids).keys() == {"20241015_0900_A"}
    assert {"session note", "delete session"} & {a["action"] for a in read_actions(bids)}


def test_crosscheck_keeps_one_of_several_runs_and_deletes_a_file(tmp_path: Path) -> None:
    bids = tmp_path / "bids"
    two_sessions(bids)
    for run in (1, 2):
        name = f"task-Matching_run-{run}_bold.nii.gz"
        write_bold(func(bids, "A", "1", name), 10)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, CrosscheckPage)
        pending = page.query_one("#pending", Table)
        assert pending.display and pending.row_count == 2
        pending.move_cursor(row=1)
        await press(app, pilot, "#keep")
        await answer(app, pilot)
        names = sorted(p.name for p in (bids / "sub-A/ses-1/func").glob("*Matching*.nii.gz"))
        assert names == ["sub-A_ses-1_task-Matching_bold.nii.gz"]
        assert not pending.display
        page.query_one("#files", Table).move_cursor(row=0)
        await settle(app, pilot)
        data = page.current_file()
        assert data is not None
        await press(app, pilot, "#delete-file")
        await answer(app, pilot)
        assert not data.path.exists()

    run_app(tmp_path, scenario, CROSSCHECK, bids=bids)


def test_a_running_job_locks_changes_but_not_notes(tmp_path: Path) -> None:
    bids = tmp_path / "bids"
    two_sessions(bids)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, CrosscheckPage)
        app.state.set_busy(CONVERTING, True)
        await settle(app, pilot)
        assert page.query_one("#delete", Button).disabled
        assert "Converting DICOMs" in line(page, "#lock")
        await submit(app, pilot, "#note", "still possible")
        assert page.reviews[0].note == "still possible"

    run_app(tmp_path, scenario, CROSSCHECK, bids=bids)


def test_overview_counts_notes_search_and_csv(tmp_path: Path) -> None:
    bids = tmp_path / "bids"
    two_sessions(bids)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, OverviewPage)
        assert "1 subject(s), 2 session(s)" in line(page, "#summary")
        await submit(app, pilot, "#note", "looked at")
        assert page.reviews[0].note == "looked at"
        table = page.query_one("#actions", Table)
        assert table.row_count == 1
        page.query_one("#search", Input).value = "nowhere"
        await settle(app, pilot)
        assert table.row_count == 0
        await press(app, pilot, "#export")
        app.screen.query_one(Input).value = str(tmp_path / "overview.csv")
        await pilot.press("enter")
        await settle(app, pilot)
        assert "2 session(s) saved" in await close_text(app, pilot)

    run_app(tmp_path, scenario, OVERVIEW, bids=bids)
    assert (tmp_path / "overview.csv").read_text(encoding="utf-8").count("\n") >= 3


# ---- Sequence overview --------------------------------------------------------------------------


def test_sequence_overview_compares_runs_and_saves(tmp_path: Path) -> None:
    bids = tmp_path / "bids"
    two_sessions(bids)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, SequencePage)
        assert page.subject() == "A" and page.scan() == "task-Rest_bold"
        assert "≠" in page.text()  # 10 and 14 volumes
        await press(app, pilot, "#save")
        app.screen.query_one(Input).value = str(tmp_path / "seq.txt")
        await pilot.press("enter")
        await settle(app, pilot)
        await press(app, pilot, "#dicom")
        assert "Choose the DICOM folder" in await close_text(app, pilot)

    run_app(tmp_path, scenario, SEQUENCE, bids=bids)
    assert "Sequence overview: sub-A" in (tmp_path / "seq.txt").read_text(encoding="utf-8")


# ---- MRIQC --------------------------------------------------------------------------------------


def test_mriqc_results_are_shown_and_rated(tmp_path: Path) -> None:
    bids, output = tmp_path / "bids", tmp_path / "mriqc"
    make_mriqc_bids(bids)
    scan = "sub-01/ses-1/func/sub-01_ses-1_task-rest_bold.nii.gz"
    write_nifti(bids / scan, 12)
    write_iqms(output, scan, fd_mean=0.2, tsnr=50.0)
    name = "sub-01_ses-1_task-rest_bold"

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, MriqcPage)
        assert [r.name for r in page.runs] == [name]
        assert page.query_one("#results", Table).texts(name)[:2] == ["sub-01", "ses-1"]
        panel = page.query_one(images.ImagePanel)
        assert panel.path is not None or "can't" in panel.message or "No" in panel.message
        page.query_one("#comment", Input).value = "spikes"
        await press(app, pilot, "#rate-1")
        assert mq.read_ratings(output)[name].rating == 1
        assert mq.read_ratings(output)[name].comment == "spikes"
        assert "bad" in line(page, "#rating-title")
        page.query_one("#comment", Input).value = ""
        await press(app, pilot, "#rate-0")
        assert name not in mq.read_ratings(output)
        await press(app, pilot, "#formulas")
        assert "Typical values" in await close_text(app, pilot)

    run_app(tmp_path, scenario, MRIQC, bids=bids, mriqc=output)


def test_mriqc_runs_the_ticked_subjects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bids, output = tmp_path / "bids", tmp_path / "mriqc"
    make_mriqc_bids(bids)
    ran: list[str] = []

    def run_subject(*args: object, **_kw: object) -> str | None:
        subject, on_line = str(args[3]), args[6]
        assert callable(on_line)
        ran.append(subject)
        on_line('[Node] Finished "mriqc.sanitize".')
        return "out of memory"

    monkeypatch.setattr(mq, "docker_status", lambda: (mq.READY, "", Path("docker")))
    monkeypatch.setattr(mq, "run_subject", run_subject)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, MriqcPage)
        assert page.subjects == ["01", "02"] and "Docker is running" in line(page, "#docker")
        assert page.resources() == (8, 30.0)  # the first time: what Docker suggests
        table = page.query_one("#subjects", TickTable)
        table.focus()
        await pilot.press("space")  # untick sub-01
        await settle(app, pilot)
        await press(app, pilot, "#run")
        assert ran == ["02"] and page.job is None and not app.state.busy
        assert any("FAILED: out of memory" in said for said in page.activity.said)
        assert table.texts("02")[-1] == "failed"

    run_app(tmp_path, scenario, MRIQC, bids=bids, mriqc=output)


# ---- C-PAC --------------------------------------------------------------------------------------


def preprocess_bids(root: Path) -> Path:
    for subject in ("P01", "P02"):
        name = "task-Rest_bold.nii.gz"
        write_bold(func(root, subject, "1", name), 10)
    (root / "dataset_description.json").write_text("{}")
    return root


def fake_docker(monkeypatch: pytest.MonkeyPatch, run_one: object) -> None:
    monkeypatch.setattr(preprocess, "find_docker", lambda: Path("docker"))
    monkeypatch.setattr(preprocess, "docker_memory_gb", lambda _docker: 64.0)
    monkeypatch.setattr(preprocess, "host_memory_gb", lambda: 64.0)
    monkeypatch.setattr(preprocess, "image_present", lambda *_: True)
    monkeypatch.setattr(preprocess, "preprocess_subject", run_one)
    monkeypatch.setattr(preprocess, "START_GAP_S", 0.0)


def test_preprocess_runs_the_ticked_subjects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    def run_one(*args: object, **_kw: object) -> str | None:
        subject, on_line = str(args[3]), args[-1]
        assert callable(on_line)
        ran.append(subject)
        on_line('[Node] Finished "anat_preproc".')
        return None

    fake_docker(monkeypatch, run_one)
    bids, output = preprocess_bids(tmp_path / "bids"), tmp_path / "out"

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, PreprocessPage)
        assert page.subjects == ["P01", "P02"] and "Docker may use" in line(page, "#memory-text")
        button = page.query_one("#process", Button)
        assert str(button.label) == "Preprocess 2 ticked subject(s)"
        page.query_one("#cpus", Input).value = "none"
        await press(app, pilot, "#process")
        assert "range" in await close_text(app, pilot)
        page.query_one("#cpus", Input).value = "1"
        page.query_one("#subjects", TickTable).focus()
        await pilot.press("space")  # untick P01
        await settle(app, pilot)
        await press(app, pilot, "#process")
        assert ran == ["P02"] and page.job is None and not app.state.busy
        assert any("sub-P02: preprocessed (1 steps)" in said for said in page.activity.said)
        assert any(said.endswith("1 preprocessed, 0 skipped (already done), 0 failed.")
                   for said in page.activity.said)  # fmt: skip
        page.query_one("#mode", FilledSelect).value = "scratch"
        await settle(app, pilot)
        await press(app, pilot, "#process")
        assert "deletes" in app.screen.text  # pyright: ignore[reportAttributeAccessIssue]
        await answer(app, pilot, "No")
        assert ran == ["P02"]

    run_app(tmp_path, scenario, PREPROCESS, bids=bids, output=output)
    settings = make_settings(tmp_path)("FmriPreprocess")
    assert settings.value("steps_per_subject/all") == 1


def test_preprocess_stops_now_and_asks_before_quitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, killed = threading.Event(), threading.Event()

    def run_one(*_args: object, **_kw: object) -> str | None:
        started.set()
        assert killed.wait(10)
        return "C-PAC exit 137"

    fake_docker(monkeypatch, run_one)
    monkeypatch.setattr(preprocess, "stop_containers", lambda *_: killed.set())
    bids, output = preprocess_bids(tmp_path / "bids"), tmp_path / "out"

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, PreprocessPage)
        page.query_one("#process", Button).press()
        while not started.is_set():
            await pilot.pause(0.02)
        await pilot.pause(0.1)
        assert page.run_box.display and "sub-P01" in page.run_box.progress.title()
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen) and "Stop C-PAC" in app.screen.text
        next(b for b in app.screen.query(Button) if str(b.label) == "Cancel").press()
        await pilot.pause()
        page.query_one("#stop-now", Button).press()
        await pilot.pause()
        await answer(app, pilot)
        assert page.statuses["P01"] == "stopped" and not app.state.busy
        assert "Stopped before it finished" in line(page, "#problems")

    run_app(tmp_path, scenario, PREPROCESS, bids=bids, output=output)


# ---- First-level --------------------------------------------------------------------------------


def test_first_level_models_at_once_and_writes_the_figures(tmp_path: Path) -> None:
    output = tmp_path / "cpac"
    write_cpac_run(output, "1", 251, amplitude=10.0, baseline=500.0, seed=1)
    write_cpac_run(output, "2", 265, amplitude=5.0, baseline=500.0, seed=2)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, FirstLevelPage)
        assert [r.run.label for r in page.model.results] == ["ses-1", "ses-2"]  # unasked
        for widget_id in ("#brain", "#times"):
            panel = page.query_one(widget_id, images.ImagePanel)
            assert panel.path is not None and panel.path.read_bytes().startswith(b"\x89PNG")
            assert str(panel.path) in panel.message  # no inline pictures here: the path
        assert page.query_one("#comparison", Table).names == ["Metric", "ses-1", "ses-2"]
        before = list(page.model.cursors)
        await submit(app, pilot, "#mni", "-9, -97")
        assert "three numbers" in line(page, "#cursor-text")
        await submit(app, pilot, "#mni", "-9 -97 -3")
        assert page.model.cursors == [(-9.0, -97.0, -3.0), before[1]]
        page.query_one("#lock", Checkbox).value = True
        await settle(app, pilot)
        assert page.model.cursors[0] == page.model.cursors[1]
        await press(app, pilot, "#save-maps")
        assert "8 map(s) saved" in await close_text(app, pilot)

    run_app(tmp_path, scenario, FIRST_LEVEL, output=output)


# ---- pieces -------------------------------------------------------------------------------------


def test_names_are_shown_as_they_are_not_as_markup(tmp_path: Path) -> None:
    bids = tmp_path / "[red]bids"
    two_sessions(bids)

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        assert app.query_one("#bids-name", Static).content == "[red]bids"
        page = page_of(app, CrosscheckPage)
        await submit(app, pilot, "#note", "[/nonsense] ^ [b]")
        assert page.table.texts("sub-A_ses-1")[-1] == "[/nonsense] ^ [b]"

    run_app(tmp_path, scenario, CROSSCHECK, bids=bids)


def test_image_download_offers_continues_and_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fmri_local_process.tui import widgets

    probe = preprocess.DockerProbe(64.0, 16, "", False)  # Docker runs, no image
    monkeypatch.setattr(preprocess, "probe_docker", lambda: probe)
    monkeypatch.setattr(widgets.PullJob, "run", lambda _job: "connection lost")
    removed: list[bool] = []
    monkeypatch.setattr(widgets, "remove", lambda _image, partial: removed.append(partial) or "")
    bids, output = preprocess_bids(tmp_path / "bids"), tmp_path / "out"

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, PreprocessPage)
        assert page.query_one("#process", Button).disabled  # no image yet
        box = page.query_one(ImageBox)
        await press(app, pilot, "#download")
        assert "Download stopped: connection lost" in line(box, "#image-text")
        assert str(box.query_one("#download", Button).label).startswith("Continue download")
        await press(app, pilot, "#remove-partial")
        await answer(app, pilot)
        assert removed == [True] and box.info.partial == ""

    run_app(tmp_path, scenario, PREPROCESS, bids=bids, output=output)
    assert image_download.NO_DOCKER  # the real calls were never made


def test_folder_box_is_locked_while_a_job_runs(tmp_path: Path) -> None:
    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        app.state.set_busy(CONVERTING, True)
        await settle(app, pilot)
        boxes = {b.key: b for b in app.query(FolderBox)}
        assert boxes["bids"].query_one(Input).disabled
        assert not boxes["behav"].query_one(Input).disabled  # only read

    run_app(tmp_path, scenario)


def test_tables_keep_the_highlighted_row_and_colours(tmp_path: Path) -> None:
    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        table = page_of(app, ConvertPage).query_one("#summary", Table)
        table.fill([("a", ("A", Text("1", style="red"))), ("b", ("B", 2))])
        table.move_cursor(row=1)
        await pilot.pause()
        table.fill([("x", ("X", 0)), ("a", ("A", 1)), ("b", ("B", 2))])
        assert table.current_key() == "b" and table.texts("b") == ["B", "2"]

    run_app(tmp_path, scenario)
