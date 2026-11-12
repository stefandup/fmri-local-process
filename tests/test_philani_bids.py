"""Tests for the Philani DICOM -> BIDS port, using fake dcm2niix/deface (no real data)."""

import json
from pathlib import Path

import pytest

from fmri_local_process.processing import philani_bids as pb
from fmri_local_process.processing.dcm2niix import Dcm2niixNotFoundError, find_dcm2niix


def make_series(root: Path, session: str, *series: str) -> Path:
    session_dir = root / session
    for name in series:
        series_dir = session_dir / "scans" / name
        series_dir.mkdir(parents=True)
        (series_dir / "img0001.dcm").write_bytes(b"fake")
    return session_dir


class FakeDcm2niix:
    def __init__(self, add_a_suffix: bool = False) -> None:
        self.calls: list[str] = []
        self.add_a_suffix = add_a_suffix

    def __call__(self, dicom_dir: Path, out_dir: Path, stem: str) -> None:
        self.calls.append(stem)
        name = stem + ("a" if self.add_a_suffix else "")
        (out_dir / f"{name}.nii.gz").write_bytes(b"nifti")
        (out_dir / f"{name}.json").write_text(json.dumps({"SeriesDescription": dicom_dir.name}))


class FakeDeface:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[Path] = []
        self.fail = fail

    def __call__(self, t1w: Path) -> None:
        self.calls.append(t1w)
        if self.fail:
            raise OSError("mri_deface crashed")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_parse_session_folder() -> None:
    session = pb.parse_session_folder(Path("20240131_ABC_P001_extra"))
    assert session is not None
    assert (session.date, session.subject_id) == ("20240131", "P001")
    assert pb.parse_session_folder(Path("notasession")) is None


@pytest.mark.parametrize(
    ("run", "expected"),
    [(None, "sub-P001_task-Rest_bold"), (2, "sub-P001_task-Rest_run-2_bold")],
)
def test_bids_stem_puts_run_before_suffix(run: int | None, expected: str) -> None:
    rule = pb.ScanRule("*_rest_AP", "func", "task-Rest", "bold")
    assert pb.bids_stem("P001", rule, run) == expected


def test_only_earliest_session_per_subject_is_converted(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240301_X_P001", "5_rest_AP")
    make_series(dicoms, "20240101_X_P001", "5_rest_AP")
    make_series(dicoms, "20240201_X_P002", "5_rest_AP")
    (dicoms / "trash").mkdir()

    fake = FakeDcm2niix()
    pb.convert_dicom_root(dicoms, bids, fake, deface=None)

    # The bash version skipped multi-session subjects entirely; P001 must still convert.
    assert sorted(fake.calls) == ["sub-P001_task-Rest_bold", "sub-P002_task-Rest_bold"]
    sidecar = read_json(bids / "sub-P001" / "func" / "sub-P001_task-Rest_bold.json")
    assert sidecar["SeriesDescription"] == "5_rest_AP"
    assert sidecar["TaskName"] == "Rest"
    assert (bids / "dataset_description.json").exists()


def test_repeated_series_get_run_numbers(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "7_Cog_reappraisal_task_AP", "9_Cog_reappraisal_task_AP")

    fake = FakeDcm2niix()
    pb.convert_dicom_root(dicoms, bids, fake, deface=None)

    assert fake.calls == [
        "sub-P001_task-Reappraisal_run-1_bold",
        "sub-P001_task-Reappraisal_run-2_bold",
    ]


def test_t1w_defaced_once_and_noted(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "2_ABCD_T1w_MPR_vNav_RMS")

    deface = FakeDeface()
    pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(add_a_suffix=True), deface)
    pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), deface)  # re-run is a no-op

    t1w_json = bids / "sub-P001" / "anat" / "sub-P001_T1w.json"
    assert len(deface.calls) == 1
    assert read_json(t1w_json)["Notes"] == pb.DEFACE_NOTE


def test_failed_deface_is_reported_and_not_noted(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "2_ABCD_T1w_MPR_vNav_RMS", "5_rest_AP")

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), FakeDeface(fail=True))

    assert [what for what, _ in report.failures] == ["sub-P001_T1w"]
    assert "Notes" not in read_json(bids / "sub-P001" / "anat" / "sub-P001_T1w.json")
    assert "sub-P001_task-Rest_bold" in report.converted  # one failure doesn't stop the rest


def test_reset_only_removes_subjects_and_crosscheck_files(tmp_path: Path) -> None:
    (tmp_path / "sub-P001" / "anat").mkdir(parents=True)
    (tmp_path / "crosscheck.json").write_text("{}")
    (tmp_path / "dataset_description.json").write_text("{}")

    pb.reset_bids_root(tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["dataset_description.json"]


def test_find_dcm2niix_explicit_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "dcm2niix.exe"
    exe.write_bytes(b"")
    assert find_dcm2niix(exe) == exe

    monkeypatch.delenv("DCM2NIIX", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        "fmri_local_process.processing.dcm2niix.REPO_THIRD_PARTY", tmp_path / "missing.exe"
    )
    with pytest.raises(Dcm2niixNotFoundError):
        find_dcm2niix()
