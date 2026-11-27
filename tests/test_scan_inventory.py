"""The scan inventory (code/scan_inventory.csv): reading it, pairing rows with sessions and
flagging run counts, on synthetic sheets and BIDS folders."""

import datetime as dt
from pathlib import Path

import pytest
from test_crosscheck import func, write_nifti

from fmri_local_process.app.crosscheck import (
    INVENTORY_CELL_CHARS,
    inventory_cell,
    inventory_notices,
    scan_inventory_text,
)
from fmri_local_process.processing import crosscheck as cc
from fmri_local_process.processing import scan_inventory as si

HEADER = (
    "Scan #,Participant ID #,Was trraining received?,Scan done?, Scan Date,T1 ,"
    "T1- Number of runs,Peer,Peer - Number of runs,Resting State,"
    "Resting State - Number of runs,fMRI - Cog Reppraisal ,Cog reappraisal - Number of runs,"
    "fMRI - Emotional labelling,Emotional la - Number of runs,DTI - AP ,"
    "DTI (AP) - Number of runs,DTI - PA ,DTI (PA) - Number of runs,Comments on scan,"
    "Report # from Dr. Lawson,fMRI report "
)


def sheet_row(subject: str, date: str, matching: str = "1", comment: str = "") -> str:
    cells = ["1", subject, "Yes", "Yes", date, "Yes", "1", "Yes", "", "Yes", "1", "Yes", "1"]
    cells += ["Yes", matching, "Yes", "1", "Yes", "1", comment, "", ""]
    return ",".join(cells)


def write_sheet(bids: Path, *rows: str, encoding: str = "utf-8-sig") -> None:
    path = bids / si.INVENTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding=encoding)


def add_session(bids: Path, subject: str, session: str, when: str, matching_runs: int) -> None:
    sub = bids / f"sub-{subject}"
    sub.mkdir(parents=True, exist_ok=True)
    tsv = sub / f"sub-{subject}_sessions.tsv"
    lines = tsv.read_text().splitlines() if tsv.exists() else ["session_id\tacq_time"]
    tsv.write_text("\n".join([*lines, f"ses-{session}\t{when}"]) + "\n")
    names = ["task-Matching_bold.nii.gz"]
    if matching_runs > 1:
        names = [f"task-Matching_run-{n}_bold.nii.gz" for n in range(1, matching_runs + 1)]
    for name in names:
        write_nifti(func(bids, subject, session, name), 10)


def test_reads_rows_with_messy_headers_twins_and_day_first_dates(tmp_path: Path) -> None:
    write_sheet(
        tmp_path,
        sheet_row("12345-1", "24/01/2025", "2 (first aborted)", "Run 1 stopped: head moved"),
        sheet_row("", "", comment="a notes-only row"),
        sheet_row("777", "sometime"),
        encoding="cp1252",
    )
    rows, problems = si.read_inventory(tmp_path)
    assert [r.subject for r in rows] == ["12345T1", "777"]
    first = rows[0]
    assert (first.line, first.date) == (2, dt.date(2025, 1, 24))
    assert first.comment == "Run 1 stopped: head moved"
    assert first.runs == {
        "T1w": 1,
        "task-Rest_bold": 1,
        "task-Reappraisal_bold": 1,
        "task-Matching_bold": 2,
    }  # Peer left blank: not compared
    assert problems == ["Scan inventory row 4 (777): date 'sometime' not read"]


def test_no_inventory_and_a_sheet_without_its_columns(tmp_path: Path) -> None:
    assert si.read_inventory(tmp_path) == ([], [])
    (tmp_path / "code").mkdir()
    (tmp_path / si.INVENTORY_FILE).write_text("Name,When\nx,y\n")
    with pytest.raises(si.InventoryError, match="participant id #"):
        si.read_inventory(tmp_path)
    (tmp_path / si.INVENTORY_FILE).write_bytes(b"PK\x03\x04" + bytes(100))  # a renamed .xlsx
    with pytest.raises(si.InventoryError, match="Excel workbook"):
        si.read_inventory(tmp_path)


def test_rows_pair_by_id_and_date_or_with_a_subjects_only_session() -> None:
    def row(line: int, subject: str, day: dt.date) -> si.InventoryRow:
        return si.InventoryRow(line, subject, subject, day)

    jan, feb, mar = dt.date(2025, 1, 1), dt.date(2025, 2, 1), dt.date(2025, 3, 1)
    sessions = {"sub-A_ses-1": ("A", jan), "sub-A_ses-2": ("A", feb), "sub-B_ses-1": ("B", jan)}
    rows = [row(2, "A", feb), row(3, "A", mar), row(4, "B", mar), row(5, "C", jan)]
    matches = si.match_inventory(rows, sessions)
    assert {name: (m.row.line, m.date_differs) for name, m in matches.items()} == {
        "sub-A_ses-2": (2, False),
        "sub-B_ses-1": (4, True),  # B's only session and only row
    }


def test_crosscheck_flags_run_counts_and_shows_the_comment(tmp_path: Path) -> None:
    add_session(tmp_path, "100", "1", "2025-01-24T10:00:00", matching_runs=2)
    add_session(tmp_path, "200", "1", "2025-02-10T09:00:00", matching_runs=1)
    add_session(tmp_path, "300", "1", "2025-03-03T09:00:00", matching_runs=1)
    write_sheet(
        tmp_path,
        sheet_row("100", "2025-01-24", "2", "Use run 2"),
        sheet_row("200", "2025-02-10", "2"),
        sheet_row("999", "2025-04-01"),
    )
    reviews = {r.name: r for r in cc.review_bids_folder(tmp_path)}

    agrees = reviews["sub-100_ses-1"]
    expected = "Scan inventory row 2 (100, 2025-01-24): Use run 2"
    assert scan_inventory_text(agrees.inventory) == expected
    assert not [i for i in agrees.issues if i.kind == "inventory" and "Matching" in i.text]

    differs = [i.text for i in reviews["sub-200_ses-1"].issues if i.kind == "inventory"]
    assert "task-Matching_bold: scan inventory says 2 run(s), BIDS has 1" in differs
    assert reviews["sub-300_ses-1"].inventory is None

    notices = inventory_notices(tmp_path, list(reviews.values()))
    assert any("row 4 (999, 2025-04-01)" in n and "match no session" in n for n in notices)
    assert "1 session(s) have no row in the scan inventory" in notices


def test_a_run_kept_at_crosscheck_isnt_flagged() -> None:
    row = si.InventoryRow(2, "A", "A", None, {"task-Matching_bold": 2, "T1w": 1})
    kept = {"task-Matching_bold"}
    assert si.run_differences(row, {"task-Matching_bold": 1, "T1w": 1}, kept) == []
    assert si.run_differences(row, {"T1w": 1}, kept) == [("task-Matching_bold", 2, 0)]


def test_the_terminal_crosscheck_shows_the_comment_and_the_banner(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from test_tui import line, page_of, run_app
    from textual.pilot import Pilot

    from fmri_local_process.app.tabs import CROSSCHECK
    from fmri_local_process.tui.crosscheck import CrosscheckPage
    from fmri_local_process.tui.toolbox import ToolboxApp
    from fmri_local_process.tui.widgets import Notices

    bids = tmp_path / "bids"
    add_session(bids, "100", "1", "2025-01-24T10:00:00", matching_runs=2)
    write_sheet(bids, sheet_row("100", "24/01/2025", "2", "Use run 2"), sheet_row("999", "x"))

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        page = page_of(app, CrosscheckPage)
        assert line(page, "#inventory") == "Scan inventory row 2 (100, 2025-01-24): Use run 2"
        assert "Use run 2" in page.table.texts("sub-100_ses-1")  # the session list's column
        banner = " ".join(page.query_one(Notices).shown)
        assert "date 'x' not read" in banner

    run_app(tmp_path, scenario, CROSSCHECK, bids=bids)


def test_the_session_lists_cell_is_one_short_line() -> None:
    def row(comment: str) -> si.InventoryRow:
        return si.InventoryRow(2, "A", "A", None, comment=comment)

    assert inventory_cell(None) == "-"
    assert inventory_cell(row("")) == "(no comment)"
    assert inventory_cell(row("run 1\naborted")) == "run 1 aborted"
    cut = inventory_cell(row("word " * 30))
    assert len(cut) <= INVENTORY_CELL_CHARS and cut.endswith("…")


def test_choosing_a_csv_checks_it_and_copies_it_in(tmp_path: Path) -> None:
    from fmri_local_process.app.crosscheck import choose_inventory, replace_inventory_question
    from fmri_local_process.processing.action_log import read_actions
    from fmri_local_process.processing.crosscheck import CrosscheckError

    bids, desktop = tmp_path / "bids", tmp_path / "Desktop"
    bids.mkdir()
    desktop.mkdir()
    workbook = desktop / "sheet.csv"
    workbook.write_bytes(b"PK\x03\x04" + bytes(50))  # an .xlsx renamed
    with pytest.raises(CrosscheckError, match="Excel workbook"):
        choose_inventory(bids, workbook)
    with pytest.raises(CrosscheckError, match="No file"):
        choose_inventory(bids, desktop / "missing.csv")
    assert not (bids / si.INVENTORY_FILE).exists()  # nothing copied in

    sheet = desktop / "inventory.csv"
    sheet.write_text("\n".join([HEADER, sheet_row("100", "2025-01-24", comment="ok")]) + "\n")
    assert replace_inventory_question(bids, sheet) == ""
    assert choose_inventory(bids, sheet) == "Scan inventory: 1 row(s) from inventory.csv"
    assert (bids / si.INVENTORY_FILE).read_bytes() == sheet.read_bytes()
    assert "Replace the scan inventory" in replace_inventory_question(bids, sheet)
    assert [a["action"] for a in read_actions(bids)] == ["scan inventory"]
    assert sheet.exists()  # the chosen sheet is left where it was


def test_the_full_note_has_the_whole_comment_and_the_runs() -> None:
    from fmri_local_process.app.crosscheck import inventory_detail

    comment = "Run 1 stopped after 3 minutes: participant moved. " * 3
    row = si.InventoryRow(5, "12345-1", "12345T1", dt.date(2025, 1, 24), {"T1w": 1}, comment)
    text = inventory_detail(row)
    assert "Row 5 of the scan inventory: 12345-1, scanned 2025-01-24" in text
    assert comment in text and "  T1w: 1" in text


def test_the_terminal_app_chooses_a_csv_on_setup_and_shows_the_full_note(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from test_tui import close_text, line, page_of, press, run_app, settle
    from textual.pilot import Pilot
    from textual.widgets import Input

    from fmri_local_process.app.tabs import CROSSCHECK, SETUP
    from fmri_local_process.tui.convert import ConvertPage
    from fmri_local_process.tui.crosscheck import CrosscheckPage
    from fmri_local_process.tui.toolbox import ToolboxApp, pane_id

    bids = tmp_path / "bids"
    add_session(bids, "100", "1", "2025-01-24T10:00:00", matching_runs=1)
    sheet = tmp_path / "inventory.csv"
    sheet.write_text("\n".join([HEADER, sheet_row("100", "2025-01-24", comment="Use run 2")]))

    async def scenario(app: ToolboxApp, pilot: Pilot) -> None:
        setup = page_of(app, ConvertPage)
        assert line(setup, "#inventory-status").startswith("None chosen yet")
        await press(app, pilot, "#inventory-csv")
        app.screen.query_one(Input).value = str(sheet)
        await pilot.press("enter")
        await settle(app, pilot)
        assert line(setup, "#inventory-status").startswith("1 row(s) in code/scan_inventory.csv")
        app.tabs.active = pane_id(CROSSCHECK)
        await settle(app, pilot)
        page = page_of(app, CrosscheckPage)
        assert "Use run 2" in line(page, "#inventory")
        await press(app, pilot, "#inventory-note")
        assert "Comments on scan:\nUse run 2" in await close_text(app, pilot)

    run_app(tmp_path, scenario, SETUP, bids=bids)
