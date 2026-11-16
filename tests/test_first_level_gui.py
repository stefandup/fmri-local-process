"""Offscreen test of the First-level stats tab on synthetic C-PAC outputs and a tiny fake
atlas cache (no Docker, no real data)."""

import os
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402 -- needs QT_QPA_PLATFORM set first
from PySide6.QtWidgets import QApplication  # noqa: E402
from test_first_level import ACTIVE, SHAPE, write_run  # noqa: E402

from fmri_local_process.gui import first_level_page, toolbox_state  # noqa: E402
from fmri_local_process.processing import atlases as at  # noqa: E402


def fake_atlas_cache(cache: Path) -> None:
    """Every cached file, on a 3 mm grid over the synthetic runs: 'Occipital Pole' where the
    synthetic response is, 'Frontal Pole' everywhere else."""
    cache.mkdir()
    affine = np.diag([3.0, 3.0, 3.0, 1.0])
    affine[:3, 3] = (-12.0, -100.0, -6.0)
    labels = np.full(SHAPE, 2, np.int16)
    labels[ACTIVE] = 1
    for spec in at.ATLASES:
        nib.save(nib.Nifti1Image(labels, affine), cache / spec.image_file)
        (cache / spec.xml_file).write_text(
            '<?xml version="1.0"?><atlas><data>'
            '<label index="0">Occipital Pole</label><label index="1">Frontal Pole</label>'
            "</data></atlas>"
        )
    nib.save(nib.Nifti1Image(np.ones(SHAPE, np.float32), affine), cache / at.TEMPLATE_FILE)


def test_first_level_tab_models_two_sessions_in_the_occipital_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        toolbox_state,
        "make_settings",
        lambda app: QSettings(str(tmp_path / f"{app}.ini"), QSettings.Format.IniFormat),
    )
    fake_atlas_cache(tmp_path / "atlases")
    monkeypatch.setattr(at, "default_cache", lambda: tmp_path / "atlases")
    output = tmp_path / "cpac"
    write_run(output, "1", 251, amplitude=10.0, baseline=500.0, seed=1)
    write_run(output, "2", 265, amplitude=5.0, baseline=500.0, seed=2)

    state = toolbox_state.ToolboxState()
    state.set_folder("output", output)
    page = first_level_page.FirstLevelPage(state)
    page.show_page()
    assert page.subject_box.currentText() == "01"
    assert page.atlas is not None and page.atlas.spec.key == "ho-cort"
    ticked = [
        page.region_list.item(i).text()
        for i in range(page.region_list.count())
        if page.region_list.item(i).checkState() == Qt.CheckState.Checked
    ]
    assert ticked == ["Occipital Pole (L)"]  # ACTIVE is all at x < 0: no right half

    page.on_run()
    assert page.worker is not None
    page.worker.wait()
    QApplication.processEvents()  # deliver the thread's done signal
    assert [r.run.label for r in page.results] == ["ses-1", "ses-2"]
    assert all(s.n_voxels == 8 for s in page.stats)  # just the 'occipital' voxels
    headers = [page.table.verticalHeaderItem(r).text() for r in range(page.table.rowCount())]  # pyright: ignore[reportOptionalMemberAccess]
    assert "Peak CNR" in headers and "C-PAC mean BOLD SNR" in headers
    assert page.cursor_mm is not None

    page.tick_none()
    assert all(s.n_voxels == 0 for s in page.stats)
    page.show_box.setCurrentText(first_level_page.SHOW_REGION)  # draws without a region
    told: list[str] = []
    monkeypatch.setattr(
        first_level_page.QMessageBox, "information", lambda *a: told.append(str(a[2]))
    )
    page.on_save_maps()
    assert told and len(list((output / "first_level").rglob("*.nii.gz"))) == 8
