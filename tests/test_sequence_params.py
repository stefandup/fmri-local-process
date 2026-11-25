"""Sequence overview: parameters from synthetic NIfTI headers, sidecars, DICOMs (no real data)."""

import json
import os
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

from fmri_local_process.processing import sequence_params as sp

STEM = "sub-P001_ses-{0}_task-Checkerboard_bold"


def write_run(bids: Path, session: str, tr: float, sidecar: dict, run: str = "") -> Path:
    stem = STEM.format(session)
    if run:
        stem = stem.replace("_bold", f"_run-{run}_bold")
    func = bids / "sub-P001" / f"ses-{session}" / "func"
    func.mkdir(parents=True, exist_ok=True)
    image = nib.Nifti1Image(np.zeros((4, 5, 6, 10), np.int16), np.diag([3.0, 3.0, 3.5, 1.0]))
    image.header.set_zooms((3.0, 3.0, 3.5, tr))
    image.header.set_xyzt_units("mm", "sec")
    nib.save(image, func / f"{stem}.nii.gz")
    (func / f"{stem}.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return func / f"{stem}.nii.gz"


def write_dicom(folder: Path, **tags: object) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(folder / "IM0001"), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = MRImageStorage
    ds.PatientName = "Secret^Person"
    for key, value in tags.items():
        setattr(ds, key, value)
    ds.save_as(folder / "IM0001", enforce_file_format=True)


@pytest.fixture
def bids(tmp_path: Path) -> Path:
    root = tmp_path / "bids"
    write_run(
        root,
        "1",
        0.832,
        {
            "RepetitionTime": 0.832,
            "EchoTime": 0.03,
            "FlipAngle": 52,
            "MultibandAccelerationFactor": 6,
            "SeriesNumber": 6,
            "SliceTiming": [0.0, 0.4, 0.0, 0.4],
        },
    )
    # ses-2: another TR, and a sidecar without the flip angle or multiband factor.
    write_run(root, "2", 0.8, {"RepetitionTime": 0.8, "EchoTime": 0.03, "SeriesNumber": 6})
    return root


def test_header_and_sidecar_values_and_differences(bids: Path) -> None:
    runs = sp.find_runs(bids, "P001")
    assert [r.column for r in runs] == ["ses-1", "ses-2"]
    assert sp.scans(runs) == ["task-Checkerboard_bold"]
    sp.read_values(runs)
    one = runs[0].values
    assert one["Matrix (voxels)"] == sp.Value("4 x 5 x 6", sp.HEADER)
    assert one["Voxel size (mm)"].text == "3 x 3 x 3.5"
    assert one["Volumes"].text == "10" and one["Duration (s)"].text == "8.3"
    assert one["TR (s)"] == sp.Value("0.832", sp.SIDECAR)
    assert one["Slice timing"].text == "4 slices, 2 shots (MB 2), ascending"

    rows = {r.label: r for r in sp.table_rows(runs)}
    assert rows["TR (s)"].differs and rows["TR in header (s)"].differs
    assert not rows["TE (s)"].differs
    assert rows["Flip angle (deg)"].incomplete and not rows["Flip angle (deg)"].differs
    assert not rows["Series number"].differs  # informative only

    lines = sp.render_table(runs, list(rows.values()), only_differences=True)
    text = "\n".join(lines)
    assert "≠ TR (s)" in text and "? Flip angle (deg)" in text and "TE (s)" not in text
    summary = "\n".join(sp.summary_lines(runs, list(rows.values())))
    assert "parameter(s) differ" in summary and "TR (s)" in summary


def test_slice_timing_orders() -> None:
    assert sp.slice_timing_summary([0, 1, 2, 3]) == "4 slices, ascending"
    assert sp.slice_timing_summary([3, 2, 1, 0]) == "4 slices, descending"
    assert sp.slice_timing_summary([0, 2, 1, 3]) == "4 slices, interleaved"


def test_dicom_fills_gaps_and_never_reads_patient_fields(bids: Path, tmp_path: Path) -> None:
    dicom = tmp_path / "dicom"
    for date, flip in (("20240101", 52), ("20240201", 60)):
        write_dicom(
            dicom / f"{date}_X_P001" / "scans" / "6_Checkerboard_task_AP",
            SeriesNumber=6,
            RepetitionTime=800 if date == "20240201" else 832,
            FlipAngle=flip,
            AcquisitionMatrix=[0, 64, 64, 0],
            PercentPhaseFieldOfView=100,
        )
    runs = sp.find_runs(bids, "P001")
    sp.read_values(runs)
    sp.add_dicom_values(runs, dicom, bids)

    one, two = (r.values for r in runs)
    assert one["Flip angle (deg)"].source == sp.SIDECAR  # the sidecar wins
    assert two["Flip angle (deg)"] == sp.Value("60", sp.DICOM)  # the gap filled
    assert two["TR (s)"].source == sp.SIDECAR
    assert one["Acquisition matrix"].text == "64 64"
    assert runs[1].dicom_note == "6_Checkerboard_task_AP"
    rows = {r.label: r for r in sp.table_rows(runs)}
    assert rows["Flip angle (deg)"].differs
    assert rows["Flip angle (deg)"].cells == ["52", "60ᵈ"]
    everything = json.dumps([{k: v.text for k, v in r.values.items()} for r in runs])
    assert "Secret" not in everything


def test_repeated_series_matched_by_series_number_or_left_out(bids: Path, tmp_path: Path) -> None:
    """A scan picked at crosscheck from two series: the sidecar's SeriesNumber tells which."""
    dicom = tmp_path / "dicom"
    session = dicom / "20240101_X_P001" / "scans"
    write_dicom(session / "6_Checkerboard_task_AP", SeriesNumber=6, FlipAngle=52)
    write_dicom(session / "8_Checkerboard_task_AP", SeriesNumber=8, FlipAngle=70)
    runs = sp.find_runs(bids, "P001")[:1]
    sp.read_values(runs)
    sp.add_dicom_values(runs, dicom, bids)
    assert runs[0].dicom_note == "6_Checkerboard_task_AP"

    sidecar = runs[0].sidecar
    sidecar.write_text(json.dumps({"RepetitionTime": 0.832}), encoding="utf-8")
    runs = sp.find_runs(bids, "P001")[:1]
    sp.read_values(runs)
    sp.add_dicom_values(runs, dicom, bids)
    assert "not guessed" in runs[0].dicom_note
    assert "Flip angle (deg)" not in runs[0].values


def test_csv_export(bids: Path, tmp_path: Path) -> None:
    runs = sp.find_runs(bids, "P001")
    sp.read_values(runs)
    out = tmp_path / "seq.csv"
    sp.write_csv(runs, sp.table_rows(runs), out)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("Group,Parameter,ses-1,ses-2,Differs")
    assert "Timing,TR (s),0.832,0.8,yes" in text.replace(os.linesep, "\n")


def test_page_shows_the_log(bids: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from fmri_local_process.gui import sequence_page, toolbox_state

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        toolbox_state,
        "make_settings",
        lambda a: QSettings(str(tmp_path / f"{a}.ini"), QSettings.Format.IniFormat),
    )
    state = toolbox_state.ToolboxState()
    state.set_folder("bids", bids)
    page = sequence_page.SequencePage(state)
    assert page.worker is not None
    page.worker.wait()
    QApplication.processEvents()
    assert page.worker is None
    text = page.log.toPlainText()
    assert text.startswith("Sequence overview: sub-P001, task-Checkerboard_bold")
    assert "≠ TR (s)" in text and "Matrix (voxels)" in text
    page.only_differences.setChecked(True)
    assert "Matrix (voxels)" not in page.log.toPlainText()
    assert page.dicom_button.isEnabled()
