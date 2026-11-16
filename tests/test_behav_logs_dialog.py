"""Offscreen test of the crosscheck's Behaviour logs dialog on synthetic logs and sessions."""

import datetime as dt
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402 -- needs QT_QPA_PLATFORM set first
from test_behav_match import emo_log  # noqa: E402 -- synthetic Presentation logs

from fmri_local_process.gui.behav_logs_dialog import BehavLogsDialog  # noqa: E402
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


def open_dialog(tmp_path: Path) -> tuple[BehavLogsDialog, Path, dict[str, int]]:
    _app = QApplication.instance() or QApplication([])
    bids, dicoms, behav = make_folders(tmp_path)
    dialog = BehavLogsDialog(bids, behav, dicoms, None)
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

    dialog = BehavLogsDialog(bids, behav, dicoms, None)
    table = dialog.session_table

    def cell(row: int, column: int) -> str:
        item = table.item(row, column)
        return item.text() if item is not None else ""

    assert [cell(1, c) for c in range(3)] == [
        "20241014_1448_12345 2",
        "2024-10-14 14:48",
        "12345 2",
    ]
    assert cell(1, 4) == "12345 2 -> 123452" and "typo" in cell(1, 8)
    assert dialog.matches[0].status == bm.UNKNOWN_ID  # the log says twin 2; no folder does yet

    dialog.session_edits["20241014_1448_12345 2"].setText("12345-2")
    dialog.recheck()
    assert dialog.matches[0].status == bm.MATCHED
    assert cell(1, 4) == "12345 2 -> 12345T2" and cell(1, 7).startswith(
        "Matching: 2024-10-14 15:10"
    )
    assert "no Matching log" in cell(0, 8)  # twin 1 still has no log
    dialog.save()
    from fmri_local_process.processing.philani_bids import read_dicom_corrections

    assert read_dicom_corrections(bids) == {"20241014_1448_12345 2": "12345-2"}
