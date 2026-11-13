"""Offscreen smoke test of the preprocess GUI on a synthetic BIDS tree (no Docker needed)."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import nibabel as nib
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402 -- needs QT_QPA_PLATFORM set first
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from fmri_local_process.gui import preprocess_gui  # noqa: E402


def no_docker() -> Path:
    raise preprocess_gui.DockerUnavailableError("Docker isn't running (test).")


@pytest.fixture
def window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[preprocess_gui.PreprocessWindow]:
    _app = QApplication.instance() or QApplication([])
    ini = tmp_path / "settings.ini"
    # Keep the test's remembered folders out of the user's real settings.
    monkeypatch.setattr(
        preprocess_gui, "QSettings", lambda *_: QSettings(str(ini), QSettings.Format.IniFormat)
    )
    monkeypatch.setattr(preprocess_gui, "find_docker", no_docker)  # never call real Docker
    window = preprocess_gui.PreprocessWindow()
    yield window
    window.memory_probe.wait()


def test_memory_minimum_and_label(window: preprocess_gui.PreprocessWindow) -> None:
    assert window.mem_spin.minimum() == preprocess_gui.MIN_MEM_GB
    assert window.mem_spin.value() >= preprocess_gui.MIN_MEM_GB
    window._on_memory_probed(4.0, "")  # Docker limited to 4 GB
    assert window.memory_label.text().startswith("⚠ Docker may only use 4.0 GB")
    window.host_gb = 32.0
    window._on_memory_probed(16.0, "")
    assert window.memory_label.text().startswith("✓ Docker may use 16.0 GB")


def test_docker_status_and_controls(
    window: preprocess_gui.PreprocessWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    window._on_memory_probed(None, "Docker isn't running (test).")
    assert window.docker_label.text().startswith("⚠ Docker isn't running")
    assert window.docker_start_button.isEnabled()

    # Preprocess warns instead of starting a batch that can only fail.
    warned: list[str] = []
    monkeypatch.setattr(
        preprocess_gui.QMessageBox, "warning", lambda _parent, title, _text: warned.append(title)
    )
    window.bids_folder = window.output_folder = Path("unused")
    window.subjects = ["P01"]
    window._on_process()
    assert warned == ["Docker isn't running"] and window.worker is None

    actions: list[str] = []
    monkeypatch.setattr(preprocess_gui, "docker_desktop", lambda action: actions.append(action))
    window._on_docker_action("start")
    assert not window.docker_restart_button.isEnabled()  # busy while it runs
    assert window.docker_control is not None
    window.docker_control.wait()
    QApplication.processEvents()  # deliver the thread's done signal
    window.memory_probe.wait()
    assert actions == ["start"] and window.docker_control is None

    window._on_memory_probed(16.0, "")
    assert window.docker_label.text() == "✓ Docker is running."
    assert not window.docker_start_button.isEnabled()
    assert window.docker_restart_button.isEnabled()


def make_bids(root: Path) -> Path:
    (root).mkdir(parents=True)
    (root / "dataset_description.json").write_text("{}")
    for rel in [
        "sub-P01/ses-1/func/sub-P01_ses-1_task-Rest_bold.nii.gz",
        "sub-P01/ses-2/func/sub-P01_ses-2_task-Matching_bold.nii.gz",
        "sub-P02/ses-1/func/sub-P02_ses-1_task-Rest_bold.nii.gz",
    ]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).touch()
    return root


def test_roster_statuses_and_task_filter(
    window: preprocess_gui.PreprocessWindow, tmp_path: Path
) -> None:
    bids = make_bids(tmp_path / "bids")
    out = tmp_path / "out"
    func = out / "output" / "pipeline_cpac_preproc" / "sub-P01" / "ses-1" / "func"
    func.mkdir(parents=True)
    (func / "sub-P01_ses-1_task-Rest_space-template_desc-preproc_bold.nii.gz").touch()
    (out / "log").mkdir()
    (out / "log" / "fmri_preprocess_sub-P02.txt").write_text("crashed\n")

    window._set_bids_folder(bids)
    window._set_output_folder(out)
    window._refresh_roster()
    assert window.subjects == ["P01", "P02"]
    # All tasks: P01's Rest is done but its Matching (ses-2) isn't, so it isn't skipped.
    assert window.statuses == {"P01": "partial", "P02": "failed"}
    window.subject_table.selectRow(0)
    texts = [label.text() for label in window.outputs_widget.findChildren(QLabel)]
    assert any(text.startswith("Still to do: task-Matching.") for text in texts)
    assert window.process_button.isEnabled()

    window.task_combo.setCurrentText("Rest")
    assert window.statuses == {"P01": "done", "P02": "failed"}

    window.subject_table.selectRow(1)
    assert window._selected_subjects() == ["P02"]
    assert "crashed" in window.log_text.toPlainText()
    texts = [label.text() for label in window.outputs_widget.findChildren(QLabel)]
    assert any(text.startswith("No known cause recognised") for text in texts)


def test_preview_tabs_show_qc_figures_and_motion(
    window: preprocess_gui.PreprocessWindow, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    func = out / "output" / "pipeline_cpac_preproc" / "sub-P01" / "ses-1" / "func"
    func.mkdir(parents=True)
    image = QImage(40, 20, QImage.Format.Format_RGB32)
    image.fill(0)
    for desc in ("movementParametersRot", "movementParametersTrans"):
        assert image.save(str(func / f"sub-P01_ses-1_task-Rest_desc-{desc}_quality.png"))
    (func / "sub-P01_ses-1_task-Rest_desc-summary_motion.tsv").write_text("MeanFD\n0.1\n")

    write_nifti(func / "sub-P01_ses-1_task-Rest_desc-mean_bold.nii.gz")
    write_nifti(func / "sub-P01_ses-1_task-Rest_desc-brain_mask.nii.gz", mask=True)

    window._set_bids_folder(make_bids(tmp_path / "bids"))
    window._set_output_folder(out)
    window._refresh_roster()
    window.subject_table.selectRow(0)
    picker = window.figure_picker
    assert picker.combo.count() == 2
    assert picker.combo.currentText() == "ses-1 Rest: Motion: rotation"
    assert picker.view.pixmap.isNull()  # hidden tab: nothing loaded yet
    window.show()
    window.detail_tabs.setCurrentWidget(picker)
    assert not picker.view.pixmap.isNull()
    picker.step(1)
    assert picker.combo.currentText() == "ses-1 Rest: Motion: translation"
    picker.step(1)  # wraps around
    assert picker.combo.currentIndex() == 0
    assert window.motion_table.rowCount() == 1
    cells = [window.motion_table.item(0, column) for column in range(2)]
    assert [cell.text() for cell in cells if cell] == ["ses-1 Rest", "0.1"]

    assert not picker.help.box.isVisible()
    picker.help.button.click()  # '?' opens notes on the figure shown
    assert picker.help.box.isVisible() and "rotation" in picker.help.box.text()
    picker.step(1)
    assert "translation" in picker.help.box.text()

    slices = window.slice_picker
    assert slices.combo.currentText() == "ses-1 Rest: Mean BOLD + BOLD mask"
    assert "functional brain mask" in slices.help.box.text()
    window.detail_tabs.setCurrentWidget(slices)
    assert not slices.view.pixmap.isNull() and len(window.slice_cache) == 1

    window.subject_table.selectRow(1)  # P02 has nothing yet
    assert picker.combo.count() == 0 and window.motion_table.rowCount() == 0
    assert slices.view.image_label.text() == "No brain images for this subject yet."
    window.close()


def write_nifti(path: Path, mask: bool = False) -> None:
    data = np.zeros((10, 12, 8), dtype=np.float32)
    data[2:8, 3:9, 2:6] = 1 if mask else 100
    nib.Nifti1Image(data, np.eye(4)).to_filename(path)


def test_read_log_tail_keeps_the_end(tmp_path: Path) -> None:
    log_file = tmp_path / "log.txt"
    log_file.write_text("\n".join(f"line {i}" for i in range(1000)))
    tail = preprocess_gui.read_log_tail(log_file, 10)
    assert tail.splitlines()[0].startswith("... 990 earlier lines")
    assert tail.splitlines()[-1] == "line 999"


def test_live_line_stays_one_line(window: preprocess_gui.PreprocessWindow) -> None:
    window._log_activity("Preprocessing 1 subject(s)...")
    window._on_subject_started(0, 1, "P01")
    for step in range(1, 50):
        window._on_step("P01", f"node_{step}", step)
    window._log_activity("note while busy")
    lines = window.activity_text.toPlainText().splitlines()
    assert len(lines) == 3
    assert lines[-1].startswith(("◐", "◓", "◑", "◒")) and "BUSY sub-P01: step 49" in lines[-1]
    assert lines[-1].endswith("node_49")

    window._log_activity("sub-P01: preprocessed (49 steps)")
    window._on_subject_finished("P01", 49, True)
    lines = window.activity_text.toPlainText().splitlines()
    assert "BUSY" not in window.activity_text.toPlainText()
    assert lines[-1].endswith("sub-P01: preprocessed (49 steps)")
    assert window.settings.value("steps_per_subject/all") in (49, "49")


def test_ticks_choose_what_runs_not_selection(
    window: preprocess_gui.PreprocessWindow, tmp_path: Path
) -> None:
    window._set_bids_folder(make_bids(tmp_path / "bids"))
    window._set_output_folder(tmp_path / "out")
    window._refresh_roster()
    assert window._ticked_subjects() == ["P01", "P02"]  # new subjects start ticked
    assert window.process_button.text() == "Preprocess 2 ticked subject(s)"

    first = window.subject_table.item(0, 0)
    assert first is not None
    first.setCheckState(Qt.CheckState.Unchecked)
    window.subject_table.selectRow(0)  # viewing P01 doesn't make it run
    assert window._ticked_subjects() == ["P02"]
    window._refresh_roster()  # ticks survive a rescan
    assert window._ticked_subjects() == ["P02"]

    window._tick_all(False)
    assert not window.process_button.isEnabled()
    window._tick_all(True)
    assert window._ticked_subjects() == ["P01", "P02"]

    # Pretend a run is going: ticks lock, rows stay clickable.
    window.worker = cast(preprocess_gui.PreprocessThread, object())
    window._update_buttons()
    item = window.subject_table.item(0, 0)
    assert item is not None
    flags = item.flags()
    assert not flags & Qt.ItemFlag.ItemIsUserCheckable
    assert flags & Qt.ItemFlag.ItemIsSelectable
    window.worker = None
