"""Offscreen test of the crosscheck window's conversion progress panel (no conversion run)."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402 -- needs QT_QPA_PLATFORM set first
from PySide6.QtWidgets import QApplication  # noqa: E402

from fmri_local_process.gui import crosscheck_gui  # noqa: E402


@pytest.fixture
def window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[crosscheck_gui.CrosscheckWindow]:
    _app = QApplication.instance() or QApplication([])
    ini = tmp_path / "settings.ini"  # keep the user's remembered folders out of the test
    monkeypatch.setattr(
        crosscheck_gui, "QSettings", lambda *_: QSettings(str(ini), QSettings.Format.IniFormat)
    )
    yield crosscheck_gui.CrosscheckWindow()


def test_progress_panel_follows_the_converter(window: crosscheck_gui.CrosscheckWindow) -> None:
    assert window.progress_group.isHidden()

    window.on_convert_progress(1, 4, "sub-P001_ses-2 from 20240301_X_P001")
    assert (window.progress_bar.value(), window.progress_bar.maximum()) == (1, 4)
    assert window.progress_bar.text() == "1 / 4 steps (25%)"
    assert window.progress_label.text() == "Now: sub-P001_ses-2 from 20240301_X_P001"

    window.on_convert_progress(4, 4, "done")
    assert window.progress_label.text() == "Finishing up..."


def test_thread_ending_without_a_report_unlocks_the_window(
    window: crosscheck_gui.CrosscheckWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DeadThread:
        def wait(self) -> None: ...

    window.progress_group.setVisible(True)
    window.convert_thread = DeadThread()  # pyright: ignore[reportAttributeAccessIssue]
    shown: list[str] = []
    monkeypatch.setattr(
        crosscheck_gui.QMessageBox, "warning", lambda _parent, _title, text: shown.append(text)
    )

    window.on_convert_finished()

    assert window.convert_thread is None and window.progress_group.isHidden()
    assert "stopped unexpectedly" in shown[0]


def test_table_shows_id_scan_date_and_session(
    window: crosscheck_gui.CrosscheckWindow, tmp_path: Path
) -> None:
    bids = tmp_path / "bids"
    func = bids / "sub-12345T1" / "ses-1" / "func"
    func.mkdir(parents=True)
    (func / "sub-12345T1_ses-1_task-Rest_run-1_bold.nii.gz").write_bytes(b"")
    (func / "sub-12345T1_ses-1_task-Rest_run-2_bold.nii.gz").write_bytes(b"")
    (bids / "sub-12345T1" / "sub-12345T1_sessions.tsv").write_text(
        "session_id\tacq_time\tsource_folder\nses-1\t2024-10-14T13:41:00\t20241014_1341_12345-1\n"
    )

    window.set_folder("bids", bids)
    window.rescan()

    cells = [window.table.item(0, c).text() for c in range(window.table.columnCount())]  # pyright: ignore[reportOptionalMemberAccess]
    assert cells[:4] == ["12345T1", "2024-10-14 13:41", "ses-1", "2"]
    assert "⚠" in cells[4]  # two runs: keep one
