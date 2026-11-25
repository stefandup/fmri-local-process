"""Offscreen test of the crosscheck's Behaviour logs dialog on synthetic logs and sessions."""

import datetime as dt
import os
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest  # noqa: E402 -- needs QT_QPA_PLATFORM set first
from PySide6.QtWidgets import QApplication  # noqa: E402
from test_behav_match import emo_log  # noqa: E402 -- synthetic Presentation logs

from fmri_local_process.app.behav import (  # noqa: E402
    S_LOGS,
    S_NOTES,
    S_RESOLVED,
    S_SESSION,
)
from fmri_local_process.gui.behav_logs_dialog import BehavLogsPanel  # noqa: E402
from fmri_local_process.processing import behav_match as bm  # noqa: E402

SESSION_DIRS = ("20240403_X_P001", "20240505_X_P002")
GOOD = "emolabeling/P001-emolabeling_scan-20240403-101500-AAAAA.log"
RESTART = "emolabeling/P001-emolabeling_scan-20240403-103000-BBBBB.log"
TYPO = "emolabeling/P02-emolabeling_scan-20240505-120000-CCCCC.log"


def make_folders(tmp_path: Path) -> tuple[Path, Path, Path]:
    bids, dicoms, behav = tmp_path / "bids", tmp_path / "dicom", tmp_path / "behav"
    bids.mkdir()
    for folder in SESSION_DIRS:
        series = dicoms / folder / "4_labeling_task_AP"
        series.mkdir(parents=True)
        (series / "img.dcm").write_bytes(b"fake")
    for key, text in ((GOOD, emo_log()), (RESTART, emo_log(False)), (TYPO, "junk\n")):
        (behav / key).parent.mkdir(parents=True, exist_ok=True)
        (behav / key).write_text(text, encoding="utf-8")
    return bids, dicoms, behav


def open_dialog(tmp_path: Path) -> tuple[BehavLogsPanel, Path, dict[str, int]]:
    _app = QApplication.instance() or QApplication([])
    bids, dicoms, behav = make_folders(tmp_path)
    dialog = BehavLogsPanel(bids, behav, dicoms, None)
    return dialog, bids, {info.key: n for n, info in enumerate(dialog.infos)}


def test_correcting_an_id_matches_the_log_and_saves(tmp_path: Path) -> None:
    dialog, bids, rows = open_dialog(tmp_path)
    typo = rows[TYPO]
    assert dialog.matches[typo].status == bm.UNKNOWN_ID
    assert "sub-P002" in dialog.matches[typo].hint  # suggests the same-day session

    dialog.id_edits[typo].setText("P002")
    dialog.date_edits[typo].setText("not a date")
    dialog.save()
    assert not (bids / bm.CORRECTIONS_FILE).exists()  # bad date: nothing saved

    dialog.date_edits[typo].setText("20240505")
    dialog.recheck()
    assert dialog.matches[typo].status == bm.MATCHED
    dialog.save()
    saved = bm.read_corrections(bids)
    assert saved == {TYPO: bm.Correction("P002", dt.date(2024, 5, 5))}


def test_view_events_mark_and_pick_one_of_the_day(tmp_path: Path) -> None:
    dialog, bids, rows = open_dialog(tmp_path)
    assert dialog.matches[rows[GOOD]].status == bm.DUPLICATE

    dialog.table.selectRow(rows[RESTART])  # the aborted restart: look at its events
    assert dialog.events_table.rowCount() == 4  # stopped before the third block fixation
    first_type = dialog.events_table.item(0, 2)
    assert first_type is not None and first_type.text() == "block_fixation"
    assert "isn't in the BIDS folder yet" in dialog.fit_label.text()
    summary = dialog.events_summary.toPlainText()
    assert "  match: 1 event(s)" in summary and "sub_acc (median, range):" in summary
    assert dialog.use_button.isEnabled()

    dialog.table.selectRow(rows[GOOD])
    dialog.use_this_one()
    assert dialog.matches[rows[GOOD]].status == bm.MATCHED
    assert dialog.matches[rows[RESTART]].status == bm.EXCLUDED

    dialog.problems_only.setChecked(True)  # both now have a verdict: nothing left but the typo
    hidden = [dialog.table.isRowHidden(rows[k]) for k in (GOOD, RESTART, TYPO)]
    assert hidden == [True, True, False]

    dialog.table.selectRow(rows[TYPO])
    assert "Can't extract events" in dialog.fit_label.text()
    dialog.note_edit.setText("empty file")
    dialog.set_verdict(bm.REVIEW_ERROR)
    dialog.save()

    saved = bm.read_corrections(bids)
    assert saved[GOOD].review == bm.REVIEW_OK
    assert saved[RESTART] == bm.Correction(
        review=bm.REVIEW_NOT_USED,
        note="P001-emolabeling_scan-20240403-101500-AAAAA.log used instead",
    )
    assert saved[TYPO] == bm.Correction(review=bm.REVIEW_ERROR, note="empty file")


def test_select_several_logs_and_mark_them_crosschecked(tmp_path: Path) -> None:
    from PySide6.QtCore import QItemSelectionModel

    dialog, bids, rows = open_dialog(tmp_path)
    assert bm.needs_review(dialog.matches[rows[TYPO]])
    dialog.table.selectRow(rows[GOOD])
    dialog.table.selectionModel().select(
        dialog.table.model().index(rows[TYPO], 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    assert dialog.mark_button.text() == "Mark 2 crosschecked"
    assert not dialog.ok_button.isEnabled()  # verdicts stay one log at a time
    assert not dialog.unmark_button.isEnabled()

    dialog.set_crosschecked(True)
    assert not bm.needs_review(dialog.matches[rows[TYPO]])
    verdict = dialog.table.item(rows[TYPO], 6)
    assert verdict is not None and verdict.text().startswith("☑")
    assert len(dialog.selected_matches()) == 2  # still selected: can un-mark straight away
    assert dialog.unmark_button.isEnabled() and not dialog.mark_button.isEnabled()
    dialog.save()
    saved = bm.read_corrections(bids)
    assert saved[GOOD] == bm.Correction(crosschecked=True)
    assert saved[TYPO].crosschecked and RESTART not in saved

    dialog.table.selectRow(rows[GOOD])
    dialog.set_crosschecked(False)
    dialog.save()
    assert GOOD not in bm.read_corrections(bids)


def test_select_several_scan_sessions_and_mark_them_crosschecked(tmp_path: Path) -> None:
    from fmri_local_process.app.behav import S_CHECKED
    from fmri_local_process.processing import philani_bids as pb

    dialog, bids, _ = open_dialog(tmp_path)
    table = dialog.session_table
    assert not dialog.mark_sessions_button.isEnabled()  # nothing selected yet
    table.selectAll()
    assert dialog.mark_sessions_button.text() == "Mark 2 crosschecked"
    assert not dialog.unmark_sessions_button.isEnabled()

    dialog.set_folders_checked(True)
    assert [table.item(r, S_CHECKED).text() for r in range(2)] == ["☑", "☑"]  # pyright: ignore[reportOptionalMemberAccess]
    assert dialog.unmark_sessions_button.isEnabled()
    assert not dialog.mark_sessions_button.isEnabled()
    dialog.save()
    assert pb.read_crosschecked_folders(bids) == set(SESSION_DIRS)

    table.selectRow([f.name for f, _ in dialog.scanned].index(SESSION_DIRS[0]))
    dialog.set_folders_checked(False)
    dialog.save()
    assert pb.read_crosschecked_folders(bids) == {SESSION_DIRS[1]}
    reopened = BehavLogsPanel(bids, tmp_path / "behav", tmp_path / "dicom", None)
    assert reopened.work.fixes.checked == {SESSION_DIRS[1]}  # read back from the BIDS folder


def test_export_destinations_and_into_bids_leaves_unplaced_out(tmp_path: Path) -> None:
    from fmri_local_process.app.behav import INTO_BIDS, INTO_OTHER, INTO_OUTPUT
    from fmri_local_process.processing.behav_export import BIDS_LAYOUT, export_events

    dialog, bids, _ = open_dialog(tmp_path)
    assert list(dialog.export_destinations().values()) == [INTO_BIDS, INTO_OTHER]
    dialog.work.output_root = tmp_path / "cpac_out"
    assert list(dialog.export_destinations().values()) == [INTO_BIDS, INTO_OUTPUT, INTO_OTHER]
    dialog.work.bids_locked = True  # view-only package
    assert INTO_BIDS not in dialog.export_destinations().values()

    # GOOD and RESTART are rival logs for one session: neither placed yet.
    report = export_events(dialog.matches, bids, BIDS_LAYOUT, skip_unplaced=True)
    assert report.written == [] and not (bids / "unplaced").exists()
    assert {why for _, why in report.left_out} == {"not matched to a scan session"}


def test_scan_sessions_tab_shows_scan_times_and_corrects_a_folder_id(tmp_path: Path) -> None:
    _app = QApplication.instance() or QApplication([])
    bids, dicoms, behav = tmp_path / "bids", tmp_path / "dicom", tmp_path / "behav"
    bids.mkdir()
    for folder in ("20241014_1341_12345-1", "20241014_1448_12345 2"):
        series = dicoms / folder / "4_labeling_task_AP"
        series.mkdir(parents=True)
        (series / "img.dcm").write_bytes(b"fake")
    twin2 = "emolabeling/12345-2-emolabeling_scan-20241014-151000-BBBBB.log"
    (behav / twin2).parent.mkdir(parents=True)
    (behav / twin2).write_text(emo_log(), encoding="utf-8")

    dialog = BehavLogsPanel(bids, behav, dicoms, None)
    table = dialog.session_table

    def cell(row: int, column: int) -> str:
        item = table.item(row, column)
        return item.text() if item is not None else ""

    assert [cell(1, c) for c in range(3)] == [
        "20241014_1448_12345 2",
        "2024-10-14 14:48",
        "12345 2",
    ]
    assert cell(1, S_RESOLVED) == "12345 2 -> 123452" and "typo" in cell(1, S_NOTES)
    assert dialog.matches[0].status == bm.UNKNOWN_ID  # the log says twin 2; no folder does yet

    dialog.session_edits["20241014_1448_12345 2"].setText("12345-2")
    dialog.recheck()
    assert dialog.matches[0].status == bm.MATCHED
    assert cell(1, S_RESOLVED) == "12345 2 -> 12345T2" and cell(1, S_LOGS).startswith(
        "Matching: 2024-10-14 15:10"
    )
    assert "no Matching log" in cell(0, S_NOTES)  # twin 1 still has no log
    dialog.save()
    from fmri_local_process.processing.philani_bids import read_dicom_corrections

    assert read_dicom_corrections(bids) == {"20241014_1448_12345 2": "12345-2"}


def test_page_loads_the_logs_and_shows_how_many_need_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtCore import QSettings

    from fmri_local_process.gui import toolbox_state
    from fmri_local_process.gui.behav_logs_dialog import BehavLogsPage

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        toolbox_state,
        "make_settings",
        lambda app: QSettings(str(tmp_path / f"{app}.ini"), QSettings.Format.IniFormat),
    )
    bids, dicoms, behav = make_folders(tmp_path)
    state = toolbox_state.ToolboxState()
    page = BehavLogsPage(state)
    badges: list[str] = []
    page.badge_changed.connect(badges.append)
    assert not page.load_button.isEnabled()  # folders not chosen yet

    for key, folder in (("bids", bids), ("dicom", dicoms), ("behav", behav)):
        state.set_folder(key, folder)
    page.show_page()  # already reading in the background since the last folder was set
    assert page.loader is not None and not page.progress_bar.isHidden()
    page.wait()

    assert page.panel is not None and len(page.panel.infos) == 3
    assert page.load_button.text() == "Reload logs"
    assert badges[-1].endswith("to review")


def test_scan_sessions_tab_shows_the_panels_sessions_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtCore import QSettings

    from fmri_local_process.gui import toolbox_state
    from fmri_local_process.gui.behav_logs_dialog import BehavLogsPage, ScanSessionsPage

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        toolbox_state,
        "make_settings",
        lambda app: QSettings(str(tmp_path / f"{app}.ini"), QSettings.Format.IniFormat),
    )
    bids, dicoms, behav = make_folders(tmp_path)
    state = toolbox_state.ToolboxState()
    page = BehavLogsPage(state)
    sessions = ScanSessionsPage(page)
    loaded: list[object] = []
    page.logs_loaded.connect(loaded.append)
    assert sessions.view is None  # nothing read yet

    for key, folder in (("bids", bids), ("dicom", dicoms), ("behav", behav)):
        state.set_folder(key, folder)
    sessions.show_page()
    page.wait()

    panel = page.panel
    assert panel is not None and sessions.view is panel.sessions_view
    assert panel.sessions_view.parent() is sessions  # on its own tab, not the logs tab's
    assert panel.session_table.rowCount() == len(SESSION_DIRS)
    assert loaded[-1] == panel.infos  # Scan lengths gets the logs to suggest lengths from

    page.load()  # reload: the old view goes, the new panel's comes in
    page.wait()
    assert page.panel is not panel and sessions.view is page.panel.sessions_view  # pyright: ignore[reportOptionalMemberAccess]


def test_scan_sessions_tab_corrects_a_folder_date_and_sets_a_session(tmp_path: Path) -> None:
    from fmri_local_process.processing import philani_bids as pb

    dialog, bids, _ = open_dialog(tmp_path)  # P001 scanned 2024-04-03, P002 2024-05-05
    table = dialog.session_table
    p002 = "20240505_X_P002"
    row = [folder.name for folder, _ in dialog.scanned].index(p002)

    date_edit, session_edit = dialog.folder_date_edits[p002], dialog.folder_session_edits[p002]
    QTest.keyClicks(date_edit, "x2024/-05-31")  # letters and slashes can't be typed
    assert date_edit.text() == "2024-05-31"
    date_edit.clear()
    QTest.keyClicks(date_edit, "2024-02-30")  # no such day: the last digit is refused
    assert date_edit.text() == "2024-02-3"
    dialog.recheck()
    assert date_edit.styleSheet()  # unfinished: marked red, nothing rechecked
    QTest.keyClicks(session_edit, "ses-0")  # only a session number from 1
    assert session_edit.text() == "0" and not session_edit.hasAcceptableInput()

    date_edit.setText("2024-05-06")
    session_edit.setText("2")  # e.g. the first visit is missing
    dialog.recheck()
    session = next(s for s in dialog.work.sessions if s.folder == p002)
    assert (session.date.isoformat(), session.label) == ("2024-05-06", "2")
    assert table.item(row, S_SESSION).text() == session.name  # pyright: ignore[reportOptionalMemberAccess]

    dialog.save()
    assert pb.read_dicom_date_corrections(bids) == {p002: date(2024, 5, 6)}
    assert pb.read_dicom_session_corrections(bids) == {p002: "2"}


def test_export_logs_one_folder_or_bids_layout(tmp_path: Path) -> None:
    from fmri_local_process.processing.behav_export import (
        BIDS_LAYOUT,
        ONE_FOLDER,
        export_events,
    )

    dialog, _bids, row_of = open_dialog(tmp_path)
    dialog.table.selectRow(row_of[GOOD])
    dialog.use_this_one()  # verdicts apply: GOOD used, RESTART "not used"

    flat = export_events(dialog.matches, tmp_path / "flat", ONE_FOLDER)

    assert [p.name for p in flat.written] == ["sub-P001_ses-1_task-Matching_events.tsv"]
    assert flat.left_out == [(RESTART, "marked 'not used'")]
    assert [log for log, _ in flat.failed] == [TYPO]  # not a Presentation log: reported
    sidecar = tmp_path / "flat" / "sub-P001_ses-1_task-Matching_events.json"
    assert "P001-emolabeling_scan-20240403-101500" in sidecar.read_text()

    nested = export_events(dialog.matches, tmp_path / "bids_out", BIDS_LAYOUT)
    assert (
        tmp_path / "bids_out" / "sub-P001" / "ses-1" / "func"
        / "sub-P001_ses-1_task-Matching_events.tsv"
    ) in nested.written  # fmt: skip
