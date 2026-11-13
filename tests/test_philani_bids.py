"""Tests for the Philani DICOM -> BIDS port, using fake dcm2niix/deface (no real data)."""

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from fmri_local_process.processing import behav_logs as bl
from fmri_local_process.processing import philani_bids as pb
from fmri_local_process.processing.behav_logs import BehavTask, EventTable
from fmri_local_process.processing.dcm2niix import (
    Dcm2niixFailedError,
    Dcm2niixNotFoundError,
    failure_summary,
    find_dcm2niix,
)


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

    def __call__(self, t1w: Path) -> str:
        self.calls.append(t1w)
        if self.fail:
            raise OSError("mri_deface crashed")
        return "FreeSurfer mri_deface"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_parse_session_folder() -> None:
    session = pb.parse_session_folder(Path("20240131_ABC_P001_extra"))
    assert session is not None
    assert (session.date, session.subject_id) == (date(2024, 1, 31), "P001")
    assert pb.parse_session_folder(Path("notasession")) is None
    assert pb.parse_session_folder(Path("20241331_ABC_P001")) is None  # month 13


def test_subject_id_made_bids_legal() -> None:
    session = pb.parse_session_folder(Path("20240131_1522_AB^CD_PILOT_"))
    assert session is not None
    assert session.subject_id == "ABCD"
    assert pb.parse_session_folder(Path("20240131_1522_^^_x")) is None


def test_subject_flag_converts_root_as_one_session(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "pilot scan", tmp_path / "bids"
    bids.mkdir()
    make_series(tmp_path, "pilot scan", "5_rest_AP", "6_Checkerboard_task_AP")

    fake = FakeDcm2niix()
    report = pb.convert_dicom_root(dicoms, bids, fake, deface=None, subject_id="PILOT-01")

    assert sorted(fake.calls) == [
        "sub-PILOT01_ses-1_task-Checkerboard_bold",
        "sub-PILOT01_ses-1_task-Rest_bold",
    ]
    assert not report.pending and not report.failures
    with pytest.raises(ValueError, match="no letters or digits"):
        pb.convert_dicom_root(dicoms, bids, fake, deface=None, subject_id="^_^")


def test_copied_series_converted_once_real_repeats_still_staged(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    session = make_series(dicoms, "20240101_X_P001", "5_rest_AP", "7_Cog_reappraisal_task_AP")
    copy = session / "Untitled"
    copy.mkdir()
    for series in (session / "scans").iterdir():
        target = copy / series.name
        target.mkdir()
        (target / "img0001.dcm").write_bytes(b"re-exported")  # same name, other size
    # A same-named series with different files is a real repeat, not a copy.
    (copy / "7_Cog_reappraisal_task_AP" / "img0002.dcm").write_bytes(b"more")

    fake = FakeDcm2niix()
    report = pb.convert_dicom_root(dicoms, bids, fake, deface=None)

    assert report.converted == ["sub-P001_ses-1_task-Rest_bold"]
    assert report.pending == [f"sub-P001_ses-1_task-Reappraisal_run-{n}_bold" for n in (1, 2)]


@pytest.mark.parametrize(
    ("run", "expected"),
    [(None, "sub-P001_ses-1_task-Rest_bold"), (2, "sub-P001_ses-1_task-Rest_run-2_bold")],
)
def test_bids_stem_puts_run_before_suffix(run: int | None, expected: str) -> None:
    assert pb.bids_stem("sub-P001_ses-1", "task-Rest", "bold", run) == expected


def test_every_session_converted_in_date_order(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240301_X_P001", "6_rest_AP")
    make_series(dicoms, "20231215_X_P001", "5_rest_AP")
    make_series(dicoms, "20240201_X_P002", "5_rest_AP")
    (dicoms / "trash").mkdir()

    fake = FakeDcm2niix()
    report = pb.convert_dicom_root(dicoms, bids, fake, deface=None)

    assert sorted(fake.calls) == [
        "sub-P001_ses-1_task-Rest_bold",
        "sub-P001_ses-2_task-Rest_bold",
        "sub-P002_ses-1_task-Rest_bold",
    ]
    func = bids / "sub-P001" / "ses-{}" / "func" / "sub-P001_ses-{}_task-Rest_bold.json"
    ses1 = read_json(Path(str(func).format(1, 1)))
    ses2 = read_json(Path(str(func).format(2, 2)))
    assert (ses1["SeriesDescription"], ses2["SeriesDescription"]) == ("5_rest_AP", "6_rest_AP")
    assert ses1["TaskName"] == "Rest"
    assert (bids / "dataset_description.json").exists()
    assert (bids / ".bidsignore").read_text() == f"{pb.PENDING_DIR}/\n"
    assert not report.pending and not report.failures


def test_repeated_series_staged_for_crosscheck(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "7_Cog_reappraisal_task_AP", "9_Cog_reappraisal_task_AP")

    fake = FakeDcm2niix()
    report = pb.convert_dicom_root(dicoms, bids, fake, deface=None)

    stems = [f"sub-P001_ses-1_task-Reappraisal_run-{n}_bold" for n in (1, 2)]
    assert fake.calls == stems
    assert report.pending == stems
    pending = bids / pb.PENDING_DIR / "sub-P001" / "ses-1" / "func"
    assert sorted(p.name for p in pending.glob("*.nii.gz")) == [f"{s}.nii.gz" for s in stems]
    final = bids / "sub-P001" / "ses-1" / "func"
    assert not final.exists()

    # Once crosscheck has put the chosen run in place, a re-run leaves it alone.
    final.mkdir(parents=True)
    (final / "sub-P001_ses-1_task-Reappraisal_bold.nii.gz").write_bytes(b"chosen")
    fake.calls.clear()
    pb.convert_dicom_root(dicoms, bids, fake, deface=None)
    assert fake.calls == []


def test_t1w_defaced_once_and_noted(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "2_ABCD_T1w_MPR_vNav_RMS")

    deface = FakeDeface()
    pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(add_a_suffix=True), deface)
    pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), deface)  # re-run is a no-op

    t1w_json = bids / "sub-P001" / "ses-1" / "anat" / "sub-P001_ses-1_T1w.json"
    assert len(deface.calls) == 1
    # Same wording as sidecars written before the defacer became selectable.
    assert read_json(t1w_json)["Notes"] == "The T1w image was defaced using FreeSurfer mri_deface"


def test_failed_deface_is_reported_and_not_noted(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "2_ABCD_T1w_MPR_vNav_RMS", "5_rest_AP")

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), FakeDeface(fail=True))

    assert [what for what, _ in report.failures] == ["sub-P001_ses-1_T1w"]
    t1w_json = bids / "sub-P001" / "ses-1" / "anat" / "sub-P001_ses-1_T1w.json"
    assert "Notes" not in read_json(t1w_json)
    assert "sub-P001_ses-1_task-Rest_bold" in report.converted  # one failure doesn't stop the rest


def fake_extract(lines: Sequence[str]) -> EventTable:
    return EventTable("sub_acc", [0.0], [100.0], [lines[0]], [1.0])


@pytest.fixture
def behav_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Behavioural log root with only the Matching task, whose 'parser' echoes line 1."""
    only_matching = (BehavTask("emolabeling", "Matching", fake_extract),)
    monkeypatch.setattr(pb, "BEHAV_TASKS", only_matching)
    monkeypatch.setattr(bl, "BEHAV_TASKS", only_matching)
    root = tmp_path / "behav"
    (root / "emolabeling").mkdir(parents=True)
    return root


def test_single_log_single_session_goes_into_func(tmp_path: Path, behav_root: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "4_labeling_task_AP")
    nested = behav_root / "emolabeling" / "2024" / "P001-run.log"  # task from a parent folder
    nested.parent.mkdir()
    nested.write_text("match\n")
    other = behav_root / "notes" / "P001-other.log"  # no task in its path: ignored
    other.parent.mkdir()
    other.write_text("not a task log\n")

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None, behav_root)

    events = bids / "sub-P001" / "ses-1" / "func" / "sub-P001_ses-1_task-Matching_events.tsv"
    assert events.read_text().splitlines()[1] == "0\t100\tmatch\t1"
    assert read_json(events.with_suffix(".json")) == {"SourceLog": "P001-run.log"}
    assert "sub-P001_ses-1_task-Matching_events" in report.converted


@pytest.mark.parametrize(
    ("sessions", "logs"),
    [
        (["20240101_X_P001"], ["P001-emolabeling.log", "P001-emolabeling-2.log"]),
        (["20240101_X_P001", "20240301_X_P001"], ["P001-emolabeling.log"]),
    ],
    ids=["two-logs", "two-sessions"],
)
def test_ambiguous_logs_staged_for_crosscheck(
    tmp_path: Path, behav_root: Path, sessions: list[str], logs: list[str]
) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    for session in sessions:
        make_series(dicoms, session, "4_labeling_task_AP")
    for name in logs:
        (behav_root / "emolabeling" / name).write_text("match\n")

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None, behav_root)

    pending = bids / pb.PENDING_DIR / "sub-P001" / "func"
    staged = sorted(p.name for p in pending.glob("*_events.tsv"))
    expected = [f"sub-P001_task-Matching_run-{n}_events.tsv" for n in range(1, len(logs) + 1)]
    assert staged == expected
    assert len(report.pending) == len(logs)
    assert not list((bids / "sub-P001").rglob("*_events.tsv"))


def test_missing_log_is_reported_not_a_failure(tmp_path: Path, behav_root: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "4_labeling_task_AP")
    make_series(dicoms, "20240101_X_P002", "5_rest_AP")  # no labeling scan: log still expected

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None, behav_root)

    assert report.missing == ["sub-P001_task-Matching", "sub-P002_task-Matching"]
    assert report.failures == []
    assert len(report.converted) == 2  # the scans still converted


def test_log_without_dicom_session_is_staged(tmp_path: Path, behav_root: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "4_labeling_task_AP")
    (behav_root / "emolabeling" / "P001-emolabeling.log").write_text("match\n")
    (behav_root / "emolabeling" / "P009-emolabeling.log").write_text("match\n")

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None, behav_root)

    staged = bids / pb.PENDING_DIR / "sub-P009" / "func"
    assert (staged / "sub-P009_task-Matching_run-1_events.tsv").exists()
    assert "sub-P009_task-Matching_run-1_events" in report.pending


def test_reset_removes_subjects_pending_and_crosscheck_files(tmp_path: Path) -> None:
    (tmp_path / "sub-P001" / "ses-1" / "anat").mkdir(parents=True)
    (tmp_path / pb.PENDING_DIR / "sub-P001").mkdir(parents=True)
    (tmp_path / "crosscheck.json").write_text("{}")
    (tmp_path / "dataset_description.json").write_text("{}")

    pb.reset_bids_root(tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["dataset_description.json"]


class FakeDcm2niixRejectingShortFiles(FakeDcm2niix):
    """Fails like real dcm2niix when a file is shorter than the others; records inputs."""

    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[list[str]] = []

    def __call__(self, dicom_dir: Path, out_dir: Path, stem: str) -> None:
        files = sorted(dicom_dir.iterdir())
        self.inputs.append([f.name for f in files])
        if len({f.stat().st_size for f in files}) > 1:
            output = "Warning: File not large enough to store image data: x.dcm\n"
            raise Dcm2niixFailedError(1, ["dcm2niix"], output)
        super().__call__(dicom_dir, out_dir, stem)


def test_truncated_files_left_out_and_reported(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    session = make_series(dicoms, "20240101_X_P001", "5_rest_AP")
    series = session / "scans" / "5_rest_AP"
    (series / "img0001.dcm").write_bytes(b"x" * 100)
    (series / "img0002.dcm").write_bytes(b"x" * 100)
    (series / "img0003.dcm").write_bytes(b"x" * 40)  # cut off mid-copy

    fake = FakeDcm2niixRejectingShortFiles()
    report = pb.convert_dicom_root(dicoms, bids, fake, deface=None)

    assert fake.inputs[-1] == ["img0001.dcm", "img0002.dcm"]
    assert report.converted == ["sub-P001_ses-1_task-Rest_bold"]
    assert [what for what, _ in report.flagged] == ["sub-P001_ses-1_task-Rest_bold"]
    assert "1 of 3 DICOM files" in report.flagged[0][1]
    assert (series / "img0003.dcm").stat().st_size == 40  # raw data untouched
    assert sorted(p.name for p in bids.iterdir()) == [
        ".bidsignore",
        "dataset_description.json",
        "sub-P001",
    ]  # nothing about it written into the BIDS folder


def test_other_dcm2niix_failures_still_fail(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "5_rest_AP")

    def broken(dicom_dir: Path, out_dir: Path, stem: str) -> None:
        raise Dcm2niixFailedError(1, ["dcm2niix"], "Error: unsupported transfer syntax\n")

    report = pb.convert_dicom_root(dicoms, bids, broken, deface=None)
    assert [what for what, _ in report.failures] == ["sub-P001_ses-1_task-Rest_bold"]


def test_deleted_scan_reconverted_in_big_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "5_rest_AP", "6_Checkerboard_task_AP")
    monkeypatch.setattr(pb, "SKIP_SESSION_ABOVE_BYTES", 0)  # every session counts as big
    pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), deface=None)

    rest = bids / "sub-P001" / "ses-1" / "func" / "sub-P001_ses-1_task-Rest_bold.nii.gz"
    rest.unlink()  # deleted at crosscheck, e.g. because it was short
    fake = FakeDcm2niix()
    pb.convert_dicom_root(dicoms, bids, fake, deface=None)
    assert fake.calls == ["sub-P001_ses-1_task-Rest_bold"]

    fake.calls.clear()
    report = pb.convert_dicom_root(dicoms, bids, fake, deface=None)
    assert fake.calls == [] and report.skipped == ["sub-P001_ses-1"]


def test_dcm2niix_failure_message_carries_its_reason() -> None:
    output = (
        "Found 495 DICOM file(s)\n"
        "FileSize < (ImageSize+HeaderSize): 524288 < (1036800+171264)\n"
        "Warning: File not large enough to store image data: x.dcm\n"
    )
    error = Dcm2niixFailedError(1, ["dcm2niix"], output)
    assert str(error) == (
        "dcm2niix exit status 1: Warning: File not large enough to store image data: x.dcm"
    )
    assert failure_summary("a\nlast line\n") == "last line"
    assert failure_summary("") == "no output"


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
