"""Tests for the BIDS-folder crosscheck checks and actions, on synthetic files."""

import gzip
import json
import os
import struct
from pathlib import Path

import pytest

from fmri_local_process.processing import crosscheck as cc
from fmri_local_process.processing.philani_bids import PENDING_DIR


def write_nifti(path: Path, volumes: int, padding: int = 0, tr: float | None = 2.0) -> Path:
    """A gzipped NIfTI-1 header (dim = [4, 2, 2, 2, volumes]) plus padding bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dims = (4, 2, 2, 2, volumes, 1, 1, 1) if volumes > 1 else (3, 2, 2, 2, 1, 1, 1, 1)
    header = struct.pack("<i", 348) + bytes(36) + struct.pack("<8h", *dims)
    header += bytes(348 - len(header))
    path.write_bytes(gzip.compress(header + os.urandom(padding)))  # incompressible
    sidecar = {"RepetitionTime": tr, "SeriesNumber": 5} if tr else {}
    path.with_name(path.name.removesuffix(".nii.gz") + ".json").write_text(json.dumps(sidecar))
    return path


def write_events(path: Path, rows: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["onset\tduration"] + [f"{i}\t1" for i in range(rows)]
    path.write_text("\n".join(lines) + "\n")
    return path


def func(bids: Path, subject: str, session: str, name: str) -> Path:
    return (
        bids / f"sub-{subject}" / f"ses-{session}" / "func" / f"sub-{subject}_ses-{session}_{name}"
    )


def review_of(bids: Path, name: str) -> cc.SessionReview:
    return next(r for r in cc.review_bids_folder(bids) if r.name == name)


def test_nifti_volumes_read_from_header(tmp_path: Path) -> None:
    assert cc.nifti_volumes(write_nifti(tmp_path / "a.nii.gz", 69)) == 69
    assert cc.nifti_volumes(write_nifti(tmp_path / "b.nii.gz", 1)) == 1
    (tmp_path / "c.nii.gz").write_bytes(b"not gzip")
    assert cc.nifti_volumes(tmp_path / "c.nii.gz") is None


def test_label_drops_subject_session_and_run() -> None:
    assert cc.label_of("sub-A_ses-1_task-Rest_run-2_bold.nii.gz") == "task-Rest_bold"
    assert cc.label_of("sub-A_task-Matching_run-1_events.tsv") == "task-Matching_events"
    assert cc.label_of("sub-A_ses-1_T1w.nii.gz") == "T1w"


def test_short_scan_found_by_comparing_with_other_sessions(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Rest_bold.nii.gz"), 495)
    write_nifti(func(tmp_path, "B", "1", "task-Rest_bold.nii.gz"), 495)
    short = write_nifti(func(tmp_path, "C", "1", "task-Rest_bold.nii.gz"), 138)

    review = review_of(tmp_path, "sub-C_ses-1")
    assert [(i.kind, i.path) for i in review.issues] == [("short", short)]
    assert "138 volumes, others have 495" in review.issues[0].text
    assert review_of(tmp_path, "sub-A_ses-1").issues == []
    assert review.files[0].summary() == "138 volumes (4.6 min)"


def test_lone_scan_has_nothing_to_compare_with(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Checkerboard_bold.nii.gz"), 69)
    assert review_of(tmp_path, "sub-A_ses-1").issues == []


def test_small_3d_image_is_short(tmp_path: Path) -> None:
    anat = tmp_path / "sub-{0}" / "ses-1" / "anat" / "sub-{0}_ses-1_T1w.nii.gz"
    for subject in "AB":
        write_nifti(Path(str(anat).format(subject)), 1, padding=100_000, tr=None)
    write_nifti(Path(str(anat).format("C")), 1, padding=10, tr=None)
    issues = review_of(tmp_path, "sub-C_ses-1").issues
    assert [i.kind for i in issues] == ["short"]


def test_missing_sidecar_events_and_common_scan(tmp_path: Path) -> None:
    for subject in "ABC":
        write_nifti(func(tmp_path, subject, "1", "task-Rest_bold.nii.gz"), 10)
    bold = write_nifti(func(tmp_path, "C", "1", "task-Matching_bold.nii.gz"), 10)
    bold.with_name(bold.name.replace(".nii.gz", ".json")).unlink()
    (func(tmp_path, "A", "1", "task-Rest_bold.nii.gz")).unlink()

    texts = [i.text for i in review_of(tmp_path, "sub-C_ses-1").issues]
    assert "sub-C_ses-1_task-Matching_bold.json (sidecar)" in texts
    assert "task-Matching_events.tsv" in texts
    missing_rest = [i.text for i in review_of(tmp_path, "sub-A_ses-1").issues]
    assert missing_rest == ["task-Rest_bold (2 of 3 sessions have it)"]


def test_empty_events_file_is_short(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Matching_bold.nii.gz"), 10)
    write_events(func(tmp_path, "A", "1", "task-Matching_events.tsv"), 0)
    issues = review_of(tmp_path, "sub-A_ses-1").issues
    assert [(i.kind, i.text) for i in issues] == [
        ("short", "sub-A_ses-1_task-Matching_events.tsv: no events")
    ]


def staged(bids: Path, subject: str, session: str, name: str) -> Path:
    return bids / PENDING_DIR / f"sub-{subject}" / f"ses-{session}" / "func" / name


def test_keep_run_moves_choice_into_place_and_deletes_others(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Rest_bold.nii.gz"), 10)  # session exists
    for run in (1, 2):
        write_nifti(staged(tmp_path, "A", "1", f"sub-A_ses-1_task-Peer_run-{run}_bold.nii.gz"), 10)

    review = review_of(tmp_path, "sub-A_ses-1")
    assert [i.kind for i in review.issues] == ["pending"]
    chosen = review.pending[1]
    target = cc.keep_run(tmp_path, chosen, review.pending)

    assert target == func(tmp_path, "A", "1", "task-Peer_bold.nii.gz")
    assert target.exists() and target.with_name("sub-A_ses-1_task-Peer_bold.json").exists()
    assert not (tmp_path / PENDING_DIR / "sub-A").exists()  # emptied staging folders removed
    assert review_of(tmp_path, "sub-A_ses-1").issues == []


def test_keep_run_refuses_to_overwrite(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Peer_bold.nii.gz"), 10)
    write_nifti(staged(tmp_path, "A", "1", "sub-A_ses-1_task-Peer_run-1_bold.nii.gz"), 10)
    review = review_of(tmp_path, "sub-A_ses-1")
    with pytest.raises(cc.CrosscheckError, match="already exists"):
        cc.keep_run(tmp_path, review.pending[0], review.pending)


def test_staged_log_assigned_to_session(tmp_path: Path) -> None:
    for session in "12":
        write_nifti(func(tmp_path, "A", session, "task-Matching_bold.nii.gz"), 10)
    log = write_events(
        tmp_path / PENDING_DIR / "sub-A" / "func" / "sub-A_task-Matching_run-1_events.tsv", 3
    )
    log.with_suffix(".json").write_text(json.dumps({"SourceLog": "A.log"}))

    logs = review_of(tmp_path, "sub-A")
    assert logs.session is None and logs.issues[0].kind == "pending"
    assert "staged, assign below" in review_of(tmp_path, "sub-A_ses-2").issues[0].text
    assert cc.sidecar_note(logs.pending[0]) == "SourceLog A.log"
    assert cc.subject_sessions(tmp_path, "A") == ["1", "2"]

    target = cc.move_into_session(tmp_path, logs.pending[0], "2")
    assert target == func(tmp_path, "A", "2", "task-Matching_events.tsv")
    assert json.loads(target.with_suffix(".json").read_text()) == {"SourceLog": "A.log"}
    assert review_of(tmp_path, "sub-A_ses-2").issues == []


def test_delete_keeps_session_folder(tmp_path: Path) -> None:
    bold = write_nifti(func(tmp_path, "A", "1", "task-Rest_bold.nii.gz"), 10)
    data = review_of(tmp_path, "sub-A_ses-1").files[0]
    cc.delete_data_file(tmp_path, data)
    assert not bold.exists() and not data.sidecar.exists()
    assert bold.parent.is_dir()  # the converter refills it


def write_bold(path: Path, volumes: int, tr: float = 2.0) -> Path:
    """A real (tiny) 4D NIfTI that nibabel can load, with a sidecar."""
    import nibabel as nib
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.arange(2 * 2 * 2 * volumes, dtype=np.int16).reshape(2, 2, 2, volumes)
    nib.save(nib.Nifti1Image(data, np.eye(4)), path)
    path.with_name(path.name.removesuffix(".nii.gz") + ".json").write_text(
        json.dumps({"RepetitionTime": tr})
    )
    return path


def test_expected_lengths_flag_short_and_long_runs(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Rest_bold.nii.gz"), 441)
    long = write_nifti(func(tmp_path, "B", "1", "task-Rest_bold.nii.gz"), 520)
    short = write_nifti(func(tmp_path, "C", "1", "task-Rest_bold.nii.gz"), 33)
    cc.write_expected(tmp_path, {"task-Rest_bold": 441, "task-Peer_bold": 0})
    assert cc.read_expected(tmp_path) == {"task-Rest_bold": 441}

    assert review_of(tmp_path, "sub-A_ses-1").issues == []
    [issue] = review_of(tmp_path, "sub-B_ses-1").issues
    assert (issue.kind, issue.path, issue.trim_to) == ("long", long, 441)
    [issue] = review_of(tmp_path, "sub-C_ses-1").issues
    assert (issue.kind, issue.path) == ("short", short)
    assert "33 volumes, expected 441" in issue.text


def test_without_expected_lengths_the_longest_runs_set_the_bar(tmp_path: Path) -> None:
    # Why the setting exists: runs stopped late make the right-length ones look short.
    for subject in "AB":
        write_nifti(func(tmp_path, subject, "1", "task-Rest_bold.nii.gz"), 520)
    write_nifti(func(tmp_path, "C", "1", "task-Rest_bold.nii.gz"), 441)
    assert [i.kind for i in review_of(tmp_path, "sub-C_ses-1").issues] == ["short"]
    cc.write_expected(tmp_path, {"task-Rest_bold": 441})
    assert review_of(tmp_path, "sub-C_ses-1").issues == []


def test_trim_keeps_the_first_volumes_and_notes_the_original(tmp_path: Path) -> None:
    import nibabel as nib
    import numpy as np

    path = write_bold(func(tmp_path, "A", "1", "task-Matching_bold.nii.gz"), 10)
    before = np.asarray(nib.Nifti1Image.from_filename(path).dataobj)
    data = next(d for d in cc.find_data_files(tmp_path) if d.path == path)

    cc.trim_run(data, 6)

    image = nib.Nifti1Image.from_filename(path)
    assert image.shape == (2, 2, 2, 6)
    assert image.get_data_dtype() == np.int16
    assert np.array_equal(np.asarray(image.dataobj), before[..., :6])
    assert json.loads(data.sidecar.read_text())["TrimmedFromVolumes"] == 10
    assert not list(path.parent.glob("*_trim*"))
    with pytest.raises(cc.CrosscheckError, match="nothing to trim"):
        cc.trim_run(data, 6)


def test_trim_refuses_to_cut_off_events(tmp_path: Path) -> None:
    path = write_bold(func(tmp_path, "A", "1", "task-Matching_bold.nii.gz"), 10, tr=2.0)
    events = func(tmp_path, "A", "1", "task-Matching_events.tsv")
    events.write_text("onset\tduration\n2\t1\n14\t2\n")  # last event ends at 16 s
    data = next(d for d in cc.find_data_files(tmp_path) if d.path == path)
    with pytest.raises(cc.CrosscheckError, match="events run to 16 s"):
        cc.trim_run(data, 6)  # 12 s
    cc.trim_run(data, 8)  # 16 s: fits
