"""Missing-volume check on synthetic DICOM series (no real data)."""

import json
import os
from dataclasses import replace
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

from fmri_local_process.processing import dicom_volumes as dv
from fmri_local_process.processing.crosscheck import DataFile, find_data_files

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def write_image(folder: Path, instance: int, z: float = 0.0, **tags: object) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    path = folder / f"IM{instance:05d}"
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = MRImageStorage
    ds.PatientName = "Secret^Person"
    ds.SeriesNumber = 6
    ds.InstanceNumber = instance
    ds.ImagePositionPatient = [0.0, 0.0, z]
    for key, value in tags.items():
        setattr(ds, key, value)
    ds.save_as(path, enforce_file_format=True)


def write_mosaic_series(folder: Path, instances: list[int], **first_tags: object) -> None:
    """One file per volume, all at the same position (a Siemens mosaic)."""
    for n, instance in enumerate(instances):
        write_image(folder, instance, 0.0, **(first_tags if n == 0 else {}))


def test_complete_series_is_ok() -> None:
    counts = dv.SeriesVolumes(images=10, first_instance=1, last_instance=10, positions=1)
    assert counts.missing_images == 0 and counts.volumes == 10


def test_gaps_in_instance_numbers_are_missing_images(tmp_path: Path) -> None:
    write_mosaic_series(tmp_path, [1, 2, 3, 5, 6, 9, 10])
    counts = dv.count_series(tmp_path)
    assert (counts.images, counts.first_instance, counts.last_instance) == (7, 1, 10)
    assert counts.missing_images == 3 and counts.volumes == 7


def test_slice_per_file_series_counts_volumes_by_position(tmp_path: Path) -> None:
    """3 slices x 4 volumes, the last volume cut after 2 slices."""
    instance = 0
    for _volume in range(4):
        for z in (0.0, 3.5, 7.0):
            instance += 1
            if instance <= 11:
                write_image(tmp_path, instance, z)
    counts = dv.count_series(tmp_path)
    assert counts.positions == 3 and counts.volumes == 3 and counts.leftover_images == 2
    assert counts.missing_images == 0


def test_planned_volumes_from_tag_or_siemens_protocol(tmp_path: Path) -> None:
    write_mosaic_series(tmp_path / "a", [1, 2, 3], NumberOfTemporalPositions=5)
    assert dv.count_series(tmp_path / "a").planned == 5
    protocol = b"### ASCCONV BEGIN ###\nlRepetitions = 299\n### ASCCONV END ###"
    folder = tmp_path / "b"
    write_image(folder, 1)
    path = folder / "IM00001"
    import pydicom

    ds = pydicom.dcmread(path)
    ds.add_new((0x0029, 0x1020), "OB", protocol)
    ds.save_as(path)
    assert dv.count_series(folder).planned == 300


def check_for(counts: dv.SeriesVolumes, nifti: int, trimmed_from: int | None = None) -> str:
    run = DataFile(Path("x_bold.nii.gz"), "P001", "1", "func", "task-x_bold", False, 0)
    run = replace(run, volumes=nifti, trimmed_from=trimmed_from)
    return dv.VolumeCheck(run, "6_x", counts).result


def test_results() -> None:
    whole = dv.SeriesVolumes(10, 1, 10, 1, planned=10)
    assert check_for(whole, 10) == dv.OK
    assert check_for(whole, 8, trimmed_from=10) == dv.OK  # trimmed on purpose
    assert check_for(whole, 9) == dv.DIFFERS
    assert check_for(dv.SeriesVolumes(8, 1, 10, 1), 8) == dv.MISSING
    assert check_for(dv.SeriesVolumes(8, 1, 8, 1, planned=10), 8) == dv.STOPPED
    assert check_for(dv.SeriesVolumes(11, 1, 11, 3), 3) == dv.INCOMPLETE


def write_bids_run(bids: Path, volumes: int) -> None:
    func = bids / "sub-P001" / "ses-1" / "func"
    func.mkdir(parents=True)
    stem = "sub-P001_ses-1_task-Checkerboard_bold"
    image = nib.Nifti1Image(np.zeros((2, 2, 2, volumes), np.int16), np.eye(4))
    nib.save(image, func / f"{stem}.nii.gz")
    (func / f"{stem}.json").write_text(json.dumps({"RepetitionTime": 1.0}), encoding="utf-8")


@pytest.fixture
def folders(tmp_path: Path) -> tuple[Path, Path]:
    bids, dicom = tmp_path / "bids", tmp_path / "dicom"
    write_bids_run(bids, 9)
    series = dicom / "20240101_X_P001" / "scans" / "6_Checkerboard_task_AP"
    write_mosaic_series(series, [1, 2, 3, 4, 5, 6, 7, 8, 10], NumberOfTemporalPositions=10)
    return bids, dicom


def test_check_runs_finds_the_series_and_the_missing_image(folders: tuple[Path, Path]) -> None:
    bids, dicom = folders
    runs = dv.bold_runs(find_data_files(bids))
    seen: list[tuple[int, int]] = []
    (check,) = dv.check_runs(bids, dicom, runs, lambda done, total: seen.append((done, total)))
    assert check.series == "6_Checkerboard_task_AP"
    assert check.result == dv.MISSING
    assert "1 image(s) missing" in check.explanation and "1-10" in check.explanation
    assert seen == [(1, 1)]
    assert "Secret" not in repr(check)


def test_box_shows_the_results(folders: tuple[Path, Path]) -> None:
    from PySide6.QtWidgets import QApplication

    from fmri_local_process.gui.dicom_volumes_box import COLUMNS, DicomVolumesBox

    _app = QApplication.instance() or QApplication([])
    bids, dicom = folders
    box = DicomVolumesBox()
    box.set_folders(bids, None, find_data_files(bids))
    assert not box.check_button.isEnabled() and "DICOM" in box.status.text()
    box.set_dicom_root(dicom)
    assert box.check_button.isEnabled()
    box.on_done(dv.check_runs(bids, dicom, box.runs), "")
    texts = [box.table.item(0, c).text() for c in range(len(COLUMNS))]  # type: ignore[union-attr]
    assert texts[2:] == ["9", "1-10", "1", "9", "10", "9", dv.MISSING]
    assert "missing" in box.status.text()
    header = box.table.horizontalHeaderItem(3)
    assert header is not None and "0020,0013" in header.toolTip()
