"""Offscreen tests of the toolbox window: its tabs, shared folders and locks, and the
Setup & Convert / BIDS Crosscheck / Scan lengths / Crosscheck overview pages (no conversion
or Docker run)."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402 -- needs QT_QPA_PLATFORM set first
from PySide6.QtWidgets import QApplication  # noqa: E402

from fmri_local_process.gui import (  # noqa: E402
    backup_box,
    convert_page,
    preprocess_gui,
    toolbox,
    toolbox_state,
)
from fmri_local_process.processing.action_log import record_action  # noqa: E402


def no_defacer() -> None:
    raise convert_page.DefaceUnavailableError("No defacer found (test).")


def no_docker() -> Path:
    raise preprocess_gui.DockerUnavailableError("Docker isn't running (test).")


@pytest.fixture
def window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[toolbox.ToolboxWindow]:
    _app = QApplication.instance() or QApplication([])

    def settings(app: str, *_rest: object) -> QSettings:  # one temp file per app
        return QSettings(str(tmp_path / f"{app}.ini"), QSettings.Format.IniFormat)

    # Keep the user's remembered folders out of the test, and never call real Docker.
    monkeypatch.setattr(toolbox_state, "make_settings", settings)
    monkeypatch.setattr(preprocess_gui, "QSettings", lambda *_: settings("FmriPreprocess"))
    monkeypatch.setattr(preprocess_gui, "find_docker", no_docker)
    monkeypatch.setattr(convert_page, "make_deface", no_defacer)  # never ask Docker
    window = toolbox.ToolboxWindow()
    yield window
    window.behav.wait()
    if window.convert.defacer_probe is not None:
        window.convert.defacer_probe.wait()
    window.preprocess.memory_probe.wait()


def two_session_subject(bids: Path) -> None:
    """sub-A with ses-1 (scanned 2024-10-14) and ses-2 (2024-10-15), each with a Rest run."""
    for session in ("1", "2"):
        func = bids / "sub-A" / f"ses-{session}" / "func"
        func.mkdir(parents=True)
        (func / f"sub-A_ses-{session}_task-Rest_bold.nii.gz").write_bytes(b"")
    (bids / "sub-A" / "sub-A_sessions.tsv").write_text(
        "session_id\tacq_time\tsource_folder\n"
        "ses-1\t2024-10-14T13:41:00\t20241014_1341_A\n"
        "ses-2\t2024-10-15T09:00:00\t20241015_0900_A\n"
    )


def open_crosscheck(window: toolbox.ToolboxWindow, bids: Path) -> None:
    window.state.set_folder("bids", bids)
    window.tabs.setCurrentIndex(toolbox.TAB_ORDER.index(toolbox.CROSSCHECK))


def test_tabs_follow_the_order_of_the_work(window: toolbox.ToolboxWindow) -> None:
    names = [window.tabs.tabText(i).replace("&&", "&") for i in range(window.tabs.count())]
    assert names == [
        "1. Setup & Convert to BIDS",
        "2. ID & Behaviour Logs",
        "3. Scan lengths & trimming",
        "4. BIDS Crosscheck",
        "5. Preprocess BIDS (C-PAC)",
        "6. Crosscheck overview",
        "7. First-level stats",
    ]


def test_folders_are_shared_and_remembered(window: toolbox.ToolboxWindow, tmp_path: Path) -> None:
    bids, output = tmp_path / "bids", tmp_path / "out"
    bids.mkdir()
    output.mkdir()
    window.state.set_folder("bids", bids)
    window.state.set_folder("output", output)

    assert window.preprocess.bids_folder == bids  # the Preprocess tab follows Setup
    assert window.preprocess.output_folder == output
    panel = window.crosscheck.bids_panel  # every BIDS folder panel follows the choice
    assert (panel.name_label.text(), panel.path_label.text()) == ("bids", f"Path: {tmp_path}\\")
    assert window.bids_name.text() == "bids"
    assert toolbox_state.ToolboxState().folder("bids") == bids  # remembered


def test_a_running_job_locks_the_other_tabs(window: toolbox.ToolboxWindow, tmp_path: Path) -> None:
    bids, dicom = tmp_path / "bids", tmp_path / "dicom"
    two_session_subject(bids)
    dicom.mkdir()
    window.state.set_folder("dicom", dicom)
    open_crosscheck(window, bids)
    assert window.convert.convert_button.isEnabled()

    window.state.set_busy(toolbox_state.PREPROCESSING, True)  # C-PAC reads the BIDS folder
    assert not window.convert.convert_button.isEnabled()
    window.crosscheck.table.item(0, 0).setCheckState(Qt.CheckState.Checked)  # pyright: ignore[reportOptionalMemberAccess]
    assert not window.crosscheck.delete_ticked_button.isEnabled()
    assert window.crosscheck.note_ticked_button.isEnabled()  # notes change no data files
    assert "C-PAC is preprocessing" in window.busy_label.text()

    window.state.set_busy(toolbox_state.PREPROCESSING, False)
    window.state.set_busy(toolbox_state.CONVERTING, True)
    assert window.preprocess.external_lock == "Converting DICOMs to BIDS"
    window.state.set_busy(toolbox_state.CONVERTING, False)
    assert window.preprocess.external_lock == ""
    assert window.crosscheck.delete_ticked_button.isEnabled()


def test_progress_panel_follows_the_converter(window: toolbox.ToolboxWindow) -> None:
    page = window.convert
    assert page.progress_group.isHidden()

    page.on_convert_progress(1, 4, "sub-P001_ses-2 from 20240301_X_P001")
    assert (page.progress_bar.value(), page.progress_bar.maximum()) == (1, 4)
    assert page.progress_bar.text() == "1 / 4 steps (25%)"
    assert page.progress_label.text() == "Now: sub-P001_ses-2 from 20240301_X_P001"

    page.on_convert_progress(4, 4, "done")
    assert page.progress_label.text() == "Finishing up..."


def test_thread_ending_without_a_report_unlocks_the_tabs(
    window: toolbox.ToolboxWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DeadThread:
        def wait(self) -> None: ...

    page = window.convert
    page.progress_group.setVisible(True)
    page.convert_thread = DeadThread()  # pyright: ignore[reportAttributeAccessIssue]
    window.state.set_busy(toolbox_state.CONVERTING, True)
    shown: list[str] = []
    monkeypatch.setattr(
        toolbox.QMessageBox, "warning", lambda _parent, _title, text: shown.append(text)
    )

    page.on_convert_finished()

    assert page.convert_thread is None and page.progress_group.isHidden()
    assert "stopped unexpectedly" in shown[0]
    assert window.state.busy == {}


def test_crosscheck_table_shows_id_scan_date_and_session(
    window: toolbox.ToolboxWindow, tmp_path: Path
) -> None:
    bids = tmp_path / "bids"
    func = bids / "sub-12345T1" / "ses-1" / "func"
    func.mkdir(parents=True)
    (func / "sub-12345T1_ses-1_task-Rest_run-1_bold.nii.gz").write_bytes(b"")
    (func / "sub-12345T1_ses-1_task-Rest_run-2_bold.nii.gz").write_bytes(b"")
    (bids / "sub-12345T1" / "sub-12345T1_sessions.tsv").write_text(
        "session_id\tacq_time\tsource_folder\nses-1\t2024-10-14T13:41:00\t20241014_1341_12345-1\n"
    )
    open_crosscheck(window, bids)

    table = window.crosscheck.table
    cells = [table.item(0, c).text() for c in range(table.columnCount())]  # pyright: ignore[reportOptionalMemberAccess]
    assert cells[:4] == ["12345T1", "2024-10-14 13:41", "ses-1", "2"]
    assert "⚠" in cells[4]  # two runs: keep one
    assert window.tabs.tabText(3) == "4. BIDS Crosscheck (1 to check)"


def test_ticked_sessions_enable_actions_and_select_none_clears(
    window: toolbox.ToolboxWindow, tmp_path: Path
) -> None:
    bids = tmp_path / "bids"
    two_session_subject(bids)
    open_crosscheck(window, bids)
    page = window.crosscheck
    assert not page.delete_ticked_button.isEnabled()
    assert "⊕" in page.table.item(0, 4).text()  # pyright: ignore[reportOptionalMemberAccess]

    for row in (0, 1):
        page.table.item(row, 0).setCheckState(Qt.CheckState.Checked)  # pyright: ignore[reportOptionalMemberAccess]
    assert [r.name for r in page.ticked_reviews()] == ["sub-A_ses-1", "sub-A_ses-2"]
    assert page.delete_ticked_button.text() == "Delete 2 session(s)..."

    page.on_select_none()
    assert page.ticked_reviews() == []
    assert not page.delete_ticked_button.isEnabled()

    window.convert.sessions_spin.setValue(2)  # this study expects two sessions: no flag
    assert "⊕" not in page.table.item(0, 4).text()  # pyright: ignore[reportOptionalMemberAccess]


def test_delete_ticked_session_after_confirming(
    window: toolbox.ToolboxWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bids = tmp_path / "bids"
    two_session_subject(bids)
    open_crosscheck(window, bids)
    page = window.crosscheck
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)  # pyright: ignore[reportOptionalMemberAccess]
    monkeypatch.setattr(page, "confirm", lambda _text: True)

    page.on_delete_ticked()

    assert not (bids / "sub-A" / "ses-1").exists()
    assert [r.name for r in page.reviews] == ["sub-A_ses-2"]
    assert page.ticked_reviews() == []


def test_scan_lengths_and_overview_pages_follow_the_bids_folder(
    window: toolbox.ToolboxWindow, tmp_path: Path
) -> None:
    bids = tmp_path / "bids"
    two_session_subject(bids)
    record_action(bids, "note", "sub-A_ses-1", "moved a lot")
    window.state.set_folder("bids", bids)

    window.tabs.setCurrentIndex(toolbox.TAB_ORDER.index(toolbox.SCAN_LENGTHS))
    panel = window.scan_lengths.panel
    assert panel is not None and list(panel.spins) == []  # empty NIfTIs have no volumes

    window.tabs.setCurrentIndex(toolbox.TAB_ORDER.index(toolbox.OVERVIEW))
    summary = window.overview.summary_label.toPlainText()
    assert "1 subject(s), 2 session(s)" in summary
    assert "⊕ 1 subject(s) with more sessions than expected" in summary
    sessions = window.overview.session_table
    assert sessions.rowCount() == 2
    header = [sessions.horizontalHeaderItem(c).text() for c in range(sessions.columnCount())]  # pyright: ignore[reportOptionalMemberAccess]
    assert header[:5] == ["ID", "Session", "Date", "T1w", "Defaced"]
    assert sessions.item(0, header.index("T1w")).text() == "0"  # pyright: ignore[reportOptionalMemberAccess]
    assert window.overview.table.item(0, 2).text() == "note"  # pyright: ignore[reportOptionalMemberAccess]
    window.overview.search.setText("nothing like this")
    assert window.overview.table.isRowHidden(0)


def test_select_all_then_mark_the_ticked_sessions_crosschecked(
    window: toolbox.ToolboxWindow, tmp_path: Path
) -> None:
    bids = tmp_path / "bids"
    two_session_subject(bids)
    open_crosscheck(window, bids)
    page = window.crosscheck
    assert window.tabs.tabText(3) == "4. BIDS Crosscheck (2 to check)"

    page.select_all_button.click()
    assert len(page.ticked_reviews()) == 2 and not page.select_all_button.isEnabled()
    assert page.mark_ticked_button.text() == "Mark 2 crosschecked"
    page.mark_ticked_button.click()

    assert all(r.crosschecked for r in page.reviews)
    assert page.table.item(0, 4).text() == "☑ crosschecked"  # pyright: ignore[reportOptionalMemberAccess]
    assert window.tabs.tabText(3) == "4. BIDS Crosscheck"  # nothing left to check
    assert page.unmark_ticked_button.isEnabled() and not page.mark_ticked_button.isEnabled()


def test_a_note_typed_in_the_overview_table_is_saved(
    window: toolbox.ToolboxWindow, tmp_path: Path
) -> None:
    from fmri_local_process.processing.crosscheck import read_notes

    bids = tmp_path / "bids"
    two_session_subject(bids)
    window.state.set_folder("bids", bids)
    window.tabs.setCurrentIndex(toolbox.TAB_ORDER.index(toolbox.OVERVIEW))
    table = window.overview.session_table
    note_column = table.columnCount() - 1

    table.item(1, note_column).setText("participant fell asleep")  # pyright: ignore[reportOptionalMemberAccess]

    assert read_notes(bids) == {"20241015_0900_A": "participant fell asleep"}
    window.tabs.setCurrentIndex(toolbox.TAB_ORDER.index(toolbox.CROSSCHECK))
    assert "participant fell asleep" in [r.note for r in window.crosscheck.reviews]


def test_deface_is_greyed_out_with_a_warning_when_no_defacer(
    window: toolbox.ToolboxWindow,
) -> None:
    page = window.convert
    if page.defacer_probe is not None:
        page.defacer_probe.wait()
    QApplication.processEvents()  # delivers the probe's answer

    assert not page.deface_box.isEnabled() and not page.deface_wanted()
    assert not page.deface_warning.isHidden()
    assert "No defacer found (test)" in page.deface_warning.toolTip()


def test_backup_box_saves_outside_the_bids_folder(
    window: toolbox.ToolboxWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fmri_local_process.processing.philani_bids import write_dicom_corrections

    monkeypatch.setattr(backup_box, "backup_base", lambda: tmp_path / "app_data")
    bids = tmp_path / "bids"
    write_dicom_corrections(bids, {"20241014_1341_A": "A"})
    window.state.set_folder("bids", bids)
    box = window.convert.backup
    box.study_edit.setText("philani pilot")

    assert box.save(manual=True)
    assert (tmp_path / "app_data" / "philani_pilot" / "latest" / "code").is_dir()
    assert box.status.text().startswith("Saved")


def test_table_mountain_sits_in_the_top_right_corner(window: toolbox.ToolboxWindow) -> None:
    assert toolbox.LOGO_IMAGE.is_file()
    logo = window.logo.pixmap()
    assert not logo.isNull() and logo.height() == toolbox.LOGO_HEIGHT


def test_scan_rules_dialog_checks_rows_previews_and_saves(
    window: toolbox.ToolboxWindow, tmp_path: Path
) -> None:
    from fmri_local_process.gui.scan_rules_dialog import ScanRulesDialog
    from fmri_local_process.processing import philani_bids as pb

    bids, dicom = tmp_path / "bids", tmp_path / "dicom"
    bids.mkdir()
    series = dicom / "Clincial Scan - 28th October 2026" / "3_t1_mprage_sag"
    series.mkdir(parents=True)
    (series / "IM0001").write_bytes(b"fake")
    (dicom / "Clincial Scan - 28th October 2026" / "7_localizer").mkdir()
    (dicom / "Clincial Scan - 28th October 2026" / "7_localizer" / "IM0001").write_bytes(b"x")
    dialog = ScanRulesDialog(bids, dicom, window)
    assert dialog.table.rowCount() == len(pb.SCAN_RULES)

    dialog.add_row("*_localizer", "localizer")  # not a BIDS scan this converter writes
    assert dialog.rules() is None
    assert "task-<Name>_bold" in dialog.table.item(dialog.table.rowCount() - 1, 1).toolTip()  # pyright: ignore[reportOptionalMemberAccess]
    dialog.table.removeRow(dialog.table.rowCount() - 1)

    dialog.check()
    report = dialog.report.toPlainText()
    assert "T1w <- 3_t1_mprage_sag" in report and "not matched: 7_localizer" in report

    dialog.save()
    assert pb.read_scan_rules(bids) == pb.SCAN_RULES
