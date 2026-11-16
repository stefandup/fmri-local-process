"""Tests for the Philani DICOM -> BIDS port, using fake dcm2niix/deface (no real data)."""

import json
import shutil
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from fmri_local_process.processing import behav_logs as bl
from fmri_local_process.processing import philani_bids as pb
from fmri_local_process.processing.action_log import read_actions
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
    assert not report.to_pick and not report.failures
    with pytest.raises(ValueError, match="no letters or digits"):
        pb.convert_dicom_root(dicoms, bids, fake, deface=None, subject_id="^_^")


def test_copied_series_converted_once_real_repeats_still_runs(tmp_path: Path) -> None:
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
    assert report.to_pick == [f"sub-P001_ses-1_task-Reappraisal_run-{n}_bold" for n in (1, 2)]


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
    assert pb.read_sessions_tsv(bids, "P001") == [
        {"session_id": "ses-1", "acq_time": "2023-12-15", "source_folder": "20231215_X_P001"},
        {"session_id": "ses-2", "acq_time": "2024-03-01", "source_folder": "20240301_X_P001"},
    ]
    assert not report.to_pick and not report.failures


def test_repeated_series_converted_as_runs_in_the_session(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "7_Cog_reappraisal_task_AP", "9_Cog_reappraisal_task_AP")
    old_staging = bids / pb.PENDING_DIR / "sub-P001"  # left by an older version
    old_staging.mkdir(parents=True)

    fake = FakeDcm2niix()
    report = pb.convert_dicom_root(dicoms, bids, fake, deface=None)

    stems = [f"sub-P001_ses-1_task-Reappraisal_run-{n}_bold" for n in (1, 2)]
    assert fake.calls == stems
    assert report.to_pick == stems
    final = bids / "sub-P001" / "ses-1" / "func"
    assert sorted(p.name for p in final.glob("*.nii.gz")) == [f"{s}.nii.gz" for s in stems]
    assert not (bids / pb.PENDING_DIR).exists()

    # Once crosscheck has kept one run (run-less name), a re-run leaves it alone.
    for stem in stems:
        (final / f"{stem}.nii.gz").unlink()
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
    # Converted in .converting/ and never moved in: no un-defaced face in the BIDS folder.
    anat = bids / "sub-P001" / "ses-1" / "anat"
    assert not anat.exists() or not list(anat.iterdir())
    assert not (bids / pb.CONVERTING_DIR).exists()
    assert "sub-P001_ses-1_task-Rest_bold" in report.converted  # one failure doesn't stop the rest

    deface = FakeDeface()
    pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), deface)  # the next run does it
    assert len(deface.calls) == 1
    assert "Notes" in read_json(anat / "sub-P001_ses-1_T1w.json")


def test_failed_conversion_leaves_no_half_written_scan(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "5_rest_AP")

    def crashes_after_writing(dicom_dir: Path, out_dir: Path, stem: str) -> None:
        (out_dir / f"{stem}.nii.gz").write_bytes(b"half")
        raise OSError("killed mid-write")

    report = pb.convert_dicom_root(dicoms, bids, crashes_after_writing, None)

    assert [what for what, _ in report.failures] == ["sub-P001_ses-1_task-Rest_bold"]
    assert not list(bids.glob("sub-P001/ses-1/func/*"))
    assert not (bids / pb.CONVERTING_DIR).exists()


def test_leftover_staging_from_a_killed_run_is_removed(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    make_series(dicoms, "20240101_X_P001", "5_rest_AP")
    leftover = bids / pb.CONVERTING_DIR / "sub-P001_ses-1_task-Rest_bold_x"
    leftover.mkdir(parents=True)
    (leftover / "sub-P001_ses-1_task-Rest_bold.nii.gz").write_bytes(b"half")

    pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None)

    assert not (bids / pb.CONVERTING_DIR).exists()
    assert (bids / "sub-P001" / "ses-1" / "func" / "sub-P001_ses-1_task-Rest_bold.nii.gz").exists()


def test_not_enough_disk_space_stops_before_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "5_rest_AP")
    monkeypatch.setattr(pb.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))

    fake = FakeDcm2niix()
    with pytest.raises(pb.NotEnoughSpaceError, match="needs about 1.0 GB"):
        pb.convert_dicom_root(dicoms, bids, fake, None)
    assert fake.calls == []


def test_excluded_session_is_left_out_and_the_rest_renumbered(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "5_rest_AP")
    make_series(dicoms, "20240102_X_P001", "5_rest_AP")
    pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None)
    assert (bids / "sub-P001" / "ses-2").is_dir()

    pb.exclude_session(bids, "20240101_X_P001", "dud")
    shutil.rmtree(bids / "sub-P001" / "ses-1")  # as the crosscheck does
    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None)

    assert report.excluded == ["20240101_X_P001"]
    assert report.renumbered == ["sub-P001: ses-2 -> ses-1"]
    assert sorted(p.name for p in (bids / "sub-P001").glob("ses-*")) == ["ses-1"]
    sessions = (bids / "sub-P001" / "sub-P001_sessions.tsv").read_text()
    assert "20240102_X_P001" in sessions and "20240101_X_P001" not in sessions
    infos = pb.scan_sessions(dicoms, excluded=pb.read_excluded_sessions(bids))
    assert [(i.folder, i.label) for i in infos] == [("20240102_X_P001", "1")]


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
def test_ambiguous_logs_not_converted_but_listed(
    tmp_path: Path, behav_root: Path, sessions: list[str], logs: list[str]
) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    for session in sessions:
        make_series(dicoms, session, "4_labeling_task_AP")
    for name in logs:
        (behav_root / "emolabeling" / name).write_text("match\n")

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None, behav_root)

    assert sorted(key for key, _ in report.unplaced) == sorted(f"emolabeling/{n}" for n in logs)
    assert "not matched to one of" in report.unplaced[0][1]
    assert not list(bids.rglob("*_events.tsv"))


def test_missing_log_is_reported_not_a_failure(tmp_path: Path, behav_root: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "4_labeling_task_AP")
    make_series(dicoms, "20240101_X_P002", "5_rest_AP")  # no labeling scan: log still expected

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None, behav_root)

    assert report.missing == ["sub-P001_task-Matching", "sub-P002_task-Matching"]
    assert report.failures == []
    assert len(report.converted) == 2  # the scans still converted


def test_log_without_dicom_session_is_listed_not_converted(
    tmp_path: Path, behav_root: Path
) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "4_labeling_task_AP")
    (behav_root / "emolabeling" / "P001-emolabeling.log").write_text("match\n")
    (behav_root / "emolabeling" / "P009-emolabeling.log").write_text("match\n")

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None, behav_root)

    assert report.unplaced == [
        ("emolabeling/P009-emolabeling.log", "no scan session with this subject ID")
    ]
    assert not (bids / "sub-P009").exists()


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


def test_twin_folder_ids_times_and_corrections(tmp_path: Path) -> None:
    session = pb.parse_session_folder(Path("20241014_1341_12345-1"))
    assert session is not None
    assert (session.subject_id, session.raw_id, session.time) == ("12345T1", "12345-1", "13:41")
    corrected = pb.parse_session_folder(Path("20241014_1448_12345 2"), corrected_id="12345-2")
    assert corrected is not None and corrected.subject_id == "12345T2"
    assert pb.parse_session_folder(Path("20241014_1500_^^"), corrected_id="P9") is not None

    pb.write_dicom_corrections(tmp_path, {"20241014_1448_12345 2": "12345-2", "x": " "})
    assert pb.read_dicom_corrections(tmp_path) == {"20241014_1448_12345 2": "12345-2"}


def test_converter_applies_dicom_id_corrections(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20241014_1341_12345-1", "5_rest_AP")
    make_series(dicoms, "20241014_1448_12345 2", "5_rest_AP")  # typo: space for the dash
    pb.write_dicom_corrections(bids, {"20241014_1448_12345 2": "12345-2"})

    pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None)

    assert sorted(p.name for p in bids.glob("sub-*")) == ["sub-12345T1", "sub-12345T2"]
    infos = pb.scan_sessions(dicoms, pb.read_dicom_corrections(bids))
    assert [(s.subject, s.raw_id, s.when, s.tasks) for s in infos] == [
        ("12345T1", "12345-1", "2024-10-14 13:41", frozenset({"Rest"})),
        ("12345T2", "12345 2", "2024-10-14 14:48", frozenset({"Rest"})),
    ]


def test_progress_reports_each_session_and_behaviour_step(tmp_path: Path, behav_root: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "4_labeling_task_AP")
    make_series(dicoms, "20240301_X_P001", "5_rest_AP")
    calls: list[tuple[int, int, str]] = []

    pb.convert_dicom_root(
        dicoms, bids, FakeDcm2niix(), None, behav_root, progress=lambda *c: calls.append(c)
    )

    assert calls == [
        (0, 4, "sub-P001_ses-1 from 20240101_X_P001"),
        (1, 4, "sub-P001_ses-2 from 20240301_X_P001"),
        (2, 4, "sub-P001: behavioural logs"),
        (3, 4, "behavioural logs without a scan session"),
        (4, 4, "done"),
    ]


def test_earlier_session_added_later_renumbers_the_converted_ones(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240301_1000_P001", "6_rest_AP")
    make_series(dicoms, "20240401_1000_P001", "7_rest_AP")
    pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None)
    (bids / "sub-P001" / "ses-1" / "func" / "note.txt").write_text("kept with its session")

    make_series(dicoms, "20240101_0900_P001", "5_rest_AP")  # an earlier scan turns up
    fake = FakeDcm2niix()
    report = pb.convert_dicom_root(dicoms, bids, fake, None)

    assert report.renumbered == ["sub-P001: ses-1 -> ses-2", "sub-P001: ses-2 -> ses-3"]
    assert fake.calls == ["sub-P001_ses-1_task-Rest_bold"]  # only the new one is converted
    func = bids / "sub-P001" / "ses-{0}" / "func" / "sub-P001_ses-{0}_task-Rest_bold.json"
    described = [read_json(Path(str(func).format(n)))["SeriesDescription"] for n in (1, 2, 3)]
    assert described == ["5_rest_AP", "6_rest_AP", "7_rest_AP"]
    assert (bids / "sub-P001" / "ses-2" / "func" / "note.txt").exists()
    assert [r["acq_time"] for r in pb.read_sessions_tsv(bids, "P001")] == [
        "2024-01-01T09:00:00",
        "2024-03-01T10:00:00",
        "2024-04-01T10:00:00",
    ]


def test_single_log_goes_to_the_only_session_even_on_another_date(
    tmp_path: Path, behav_root: Path
) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "4_labeling_task_AP")
    (behav_root / "emolabeling" / "P001-emolabeling_scan-20240105-101500-AAAAA.log").write_text(
        "match\n"
    )

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None, behav_root)

    events = bids / "sub-P001" / "ses-1" / "func" / "sub-P001_ses-1_task-Matching_events.tsv"
    assert read_json(events.with_suffix(".json"))["SourceLogDate"] == "2024-01-05"
    assert report.unplaced == []


def test_folder_date_and_session_corrections_reach_the_conversion(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20240101_X_P001", "5_rest_AP")
    make_series(dicoms, "20240301_X_P001", "5_rest_AP")
    # The first folder's name has the wrong date (really scanned after the second), and the
    # study's first visit is missing, so the later one is set to be ses-3.
    pb.write_dicom_date_corrections(bids, {"20240101_X_P001": date(2024, 6, 1)})
    pb.write_dicom_session_corrections(bids, {"20240101_X_P001": "3"})

    pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None)

    rows = (bids / "sub-P001" / "sub-P001_sessions.tsv").read_text().splitlines()[1:]
    assert rows == [
        "ses-1\t2024-03-01\t20240301_X_P001",
        "ses-3\t2024-06-01\t20240101_X_P001",
    ]
    assert [a["action"] for a in read_actions(bids)][:2] == [
        "correct DICOM folder date",
        "set DICOM folder session",
    ]


def test_a_session_number_set_twice_keeps_the_earlier(caplog: pytest.LogCaptureFixture) -> None:
    sessions = [
        pb.DicomSession(Path(name), date(2024, month, 1), "P001")
        for name, month in (("a", 1), ("b", 2), ("c", 3))
    ]
    numbered = pb.number_sessions(sessions, {"a": "2", "b": "2"})["P001"]
    assert [(s.dicom.folder.name, s.label) for s in numbered] == [
        ("a", "2"),
        ("b", "1"),
        ("c", "3"),
    ]
    assert "also set to ses-2" in caplog.text


def test_a_folder_named_anyhow_converts_with_a_corrected_id_and_date(tmp_path: Path) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    odd = "Clincial Scan - 28th October 2026"
    make_series(dicoms, odd, "5_rest_AP")
    assert pb.find_sessions(dicoms) == []  # the name alone says nothing usable
    pb.write_dicom_corrections(bids, {odd: "FATIMA"})
    pb.write_dicom_date_corrections(bids, {odd: date(2026, 10, 28)})
    pb.write_dicom_session_corrections(bids, {odd: "1"})

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None)

    assert "sub-FATIMA_ses-1_task-Rest_bold" in report.converted
    rows = (bids / "sub-FATIMA" / "sub-FATIMA_sessions.tsv").read_text().splitlines()
    assert rows[1] == f"ses-1\t2026-10-28\t{odd}"
    only_id = pb.parse_session_folder(dicoms / odd, corrected_id="FATIMA")
    assert only_id is None  # an ID alone isn't enough: when was it scanned?


def test_a_session_with_no_recognised_series_is_reported_with_its_series(
    tmp_path: Path,
) -> None:
    dicoms, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    make_series(dicoms, "20261028_0900_P001", "3_t1_mprage_sag", "7_ep2d_bold_clinical")
    make_series(dicoms, "20240101_X_P002", "2_ABCD_T1w_MPR_vNav_RMS")

    report = pb.convert_dicom_root(dicoms, bids, FakeDcm2niix(), None)

    assert report.no_scans == [("20261028_0900_P001", "3_t1_mprage_sag, 7_ep2d_bold_clinical")]
    assert not (bids / "sub-P001" / "ses-1").exists()
    # The scan sessions view lists the T1w too (not only the BOLD tasks).
    scanned = dict((folder.name, scans) for folder, scans in pb.scan_dicom_root(dicoms))
    assert scanned == {"20240101_X_P002": frozenset({"T1w"}), "20261028_0900_P001": frozenset()}
