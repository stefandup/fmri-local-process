"""Tests for the BIDS-folder crosscheck checks and actions, on synthetic files."""

import gzip
import json
import os
import struct
from pathlib import Path

import pytest

from fmri_local_process.processing import crosscheck as cc
from fmri_local_process.processing.action_log import read_actions
from fmri_local_process.processing.philani_bids import read_excluded_sessions


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
    assert "138 volumes, median of runs 495" in review.issues[0].text
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
    bold = write_nifti(func(tmp_path, "C", "1", "task-Matching_bold.nii.gz"), 495)
    bold.with_name(bold.name.replace(".nii.gz", ".json")).unlink()
    (func(tmp_path, "A", "1", "task-Rest_bold.nii.gz")).unlink()

    texts = [i.text for i in review_of(tmp_path, "sub-C_ses-1").issues]
    assert "sub-C_ses-1_task-Matching_bold.json (sidecar)" in texts
    assert "task-Matching_events.tsv: no log placed here (see IDs & behaviour logs)" in texts
    missing_rest = [i.text for i in review_of(tmp_path, "sub-A_ses-1").issues]
    assert missing_rest == ["task-Rest_bold (2 of 3 sessions have it)"]


def test_empty_events_file_is_short(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Matching_bold.nii.gz"), 495)
    write_events(func(tmp_path, "A", "1", "task-Matching_events.tsv"), 0)
    issues = review_of(tmp_path, "sub-A_ses-1").issues
    assert [(i.kind, i.text) for i in issues] == [
        ("short", "sub-A_ses-1_task-Matching_events.tsv: no events")
    ]


def test_keep_run_renames_choice_and_deletes_the_other_runs(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Rest_bold.nii.gz"), 495)
    for run in (1, 2):
        write_nifti(func(tmp_path, "A", "1", f"task-Peer_run-{run}_bold.nii.gz"), 140)

    review = review_of(tmp_path, "sub-A_ses-1")
    assert [(i.kind, i.text) for i in review.issues] == [
        ("pending", "task-Peer_bold: 2 runs, keep one")
    ]
    assert [d.path.name for d in review.pending] == [
        "sub-A_ses-1_task-Peer_run-1_bold.nii.gz",
        "sub-A_ses-1_task-Peer_run-2_bold.nii.gz",
    ]
    chosen = review.pending[1]
    target = cc.keep_run(tmp_path, chosen, review.pending)

    assert target == func(tmp_path, "A", "1", "task-Peer_bold.nii.gz")
    assert target.exists() and target.with_name("sub-A_ses-1_task-Peer_bold.json").exists()
    assert not list(target.parent.glob("*_run-*"))
    assert review_of(tmp_path, "sub-A_ses-1").issues == []


def test_keep_run_refuses_to_overwrite(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Peer_bold.nii.gz"), 10)
    write_nifti(func(tmp_path, "A", "1", "task-Peer_run-1_bold.nii.gz"), 10)
    review = review_of(tmp_path, "sub-A_ses-1")
    with pytest.raises(cc.CrosscheckError, match="already exists"):
        cc.keep_run(tmp_path, review.pending[0], review.pending)


def test_session_date_from_sessions_tsv_and_log_date_check(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Matching_bold.nii.gz"), 495)
    events = write_events(func(tmp_path, "A", "1", "task-Matching_events.tsv"), 3)
    events.with_suffix(".json").write_text(
        json.dumps({"SourceLog": "A.log", "SourceLogDate": "2024-10-15"})
    )
    (tmp_path / "sub-A" / "sub-A_sessions.tsv").write_text(
        "session_id\tacq_time\tsource_folder\nses-1\t2024-10-14T13:41:00\t20241014_1341_A\n"
    )

    review = review_of(tmp_path, "sub-A_ses-1")

    assert (review.date, review.source_folder) == ("2024-10-14 13:41", "20241014_1341_A")
    assert [(i.kind, i.text) for i in review.issues] == [
        (
            "check",
            "sub-A_ses-1_task-Matching_events.tsv: from a log dated 2024-10-15, "
            "scanned 2024-10-14 (A.log)",
        )
    ]
    assert cc.sidecar_note(review.files[1]) == "SourceLog A.log"


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

    cc.trim_run(tmp_path, data, 6)

    image = nib.Nifti1Image.from_filename(path)
    assert image.shape == (2, 2, 2, 6)
    assert image.get_data_dtype() == np.int16
    assert np.array_equal(np.asarray(image.dataobj), before[..., :6])
    assert json.loads(data.sidecar.read_text())["TrimmedFromVolumes"] == 10
    assert not list(path.parent.glob("*_trim*"))
    [logged] = read_actions(tmp_path)
    assert (logged["action"], logged["target"], logged["detail"]) == (
        "trim",
        "sub-A/ses-1/func/sub-A_ses-1_task-Matching_bold.nii.gz",
        "10 -> 6 volumes",
    )
    with pytest.raises(cc.CrosscheckError, match="nothing to trim"):
        cc.trim_run(tmp_path, data, 6)


def test_trim_refuses_to_cut_off_events(tmp_path: Path) -> None:
    path = write_bold(func(tmp_path, "A", "1", "task-Matching_bold.nii.gz"), 10, tr=2.0)
    events = func(tmp_path, "A", "1", "task-Matching_events.tsv")
    events.write_text("onset\tduration\n2\t1\n14\t2\n")  # last event ends at 16 s
    data = next(d for d in cc.find_data_files(tmp_path) if d.path == path)
    with pytest.raises(cc.CrosscheckError, match="events run to 16 s"):
        cc.trim_run(tmp_path, data, 6)  # 12 s
    cc.trim_run(tmp_path, data, 8)  # 16 s: fits


def test_median_of_the_runs_is_the_default_expected_length(tmp_path: Path) -> None:
    for subject, volumes in (("A", 441), ("B", 441), ("C", 520), ("D", 33)):
        write_nifti(func(tmp_path, subject, "1", "task-Rest_bold.nii.gz"), volumes)

    assert cc.median_volumes(cc.find_data_files(tmp_path)) == {"task-Rest_bold": 441}
    [long] = review_of(tmp_path, "sub-C_ses-1").issues
    assert (long.kind, long.trim_to) == ("long", 441)
    assert "median of runs 441" in long.text
    assert [i.kind for i in review_of(tmp_path, "sub-D_ses-1").issues] == ["short"]

    cc.write_expected(tmp_path, {"task-Rest_bold": 520})  # the human's number wins
    assert review_of(tmp_path, "sub-C_ses-1").issues == []
    assert "expected 520" in review_of(tmp_path, "sub-A_ses-1").issues[0].text


def test_two_runs_one_cut_short_never_offers_trimming_the_full_one(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Rest_bold.nii.gz"), 441)
    write_nifti(func(tmp_path, "B", "1", "task-Rest_bold.nii.gz"), 33)
    assert review_of(tmp_path, "sub-A_ses-1").issues == []
    assert [i.kind for i in review_of(tmp_path, "sub-B_ses-1").issues] == ["short"]


def test_expected_lengths_dialog_defaults_to_median_and_saves_overrides(tmp_path: Path) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from fmri_local_process.gui.scan_lengths import ScanLengthsPanel

    _app = QApplication.instance() or QApplication([])
    for subject, volumes in (("A", 441), ("B", 441), ("C", 520)):
        write_nifti(func(tmp_path, subject, "1", "task-Rest_bold.nii.gz"), volumes)
        write_nifti(func(tmp_path, subject, "1", "task-Peer_bold.nii.gz"), 100)

    dialog = ScanLengthsPanel(tmp_path, cc.find_data_files(tmp_path), None)
    rest, peer = dialog.spins["task-Rest_bold"], dialog.spins["task-Peer_bold"]
    assert (rest.value(), rest.specialValueText()) == (0, "median (441)")
    assert dialog.trim_button.text() == "Trim 1 long run(s)..."  # C: 520 > 441
    rest.setValue(520)  # typed: nothing is too long any more
    assert not dialog.trim_button.isEnabled()
    rest.setValue(0)
    peer.setValue(90)  # a number set by hand is remembered; the median isn't stored
    dialog.save()  # also saves by itself once typing pauses
    assert cc.read_expected(tmp_path) == {"task-Peer_bold": 90}

    reopened = ScanLengthsPanel(tmp_path, cc.find_data_files(tmp_path), None, can_trim=False)
    assert reopened.spins["task-Peer_bold"].value() == 90
    assert not reopened.trim_button.isEnabled()  # a trim is already running
    reopened.trim()
    assert reopened.trim_requested


def two_sessions(bids: Path) -> None:
    for session in ("1", "2"):
        write_nifti(func(bids, "A", session, "task-Rest_bold.nii.gz"), 495)
    (bids / "sub-A" / "sub-A_sessions.tsv").write_text(
        "session_id\tacq_time\tsource_folder\n"
        "ses-1\t2024-10-14T13:41:00\t20241014_1341_A\n"
        "ses-2\t2024-10-15T09:00:00\t20241015_0900_A\n"
    )


def test_more_sessions_than_expected_are_flagged(tmp_path: Path) -> None:
    two_sessions(tmp_path)
    assert cc.read_expected_sessions(tmp_path) == 1  # the default
    [issue] = review_of(tmp_path, "sub-A_ses-2").issues
    assert issue.kind == "extra"
    assert issue.text == (
        "sub-A has 2 sessions, 1 expected (ses-1 2024-10-14 13:41, ses-2 2024-10-15 09:00)"
    )

    cc.write_expected_sessions(tmp_path, 2)
    assert cc.read_expected_sessions(tmp_path) == 2
    assert review_of(tmp_path, "sub-A_ses-1").issues == []
    assert read_actions(tmp_path)[-1]["detail"] == "1 -> 2"


def test_lone_scan_compared_with_the_study_default(tmp_path: Path) -> None:
    short = write_nifti(func(tmp_path, "A", "1", "task-Rest_bold.nii.gz"), 300)
    [issue] = review_of(tmp_path, "sub-A_ses-1").issues
    assert (issue.kind, issue.path) == ("short", short)
    assert "300 volumes, study default 495" in issue.text
    # Two runs or more: their median wins over the study default.
    write_nifti(func(tmp_path, "B", "1", "task-Rest_bold.nii.gz"), 300)
    assert review_of(tmp_path, "sub-A_ses-1").issues == []


def test_notes_follow_the_dicom_folder_and_are_logged(tmp_path: Path) -> None:
    two_sessions(tmp_path)
    reviews = [r for r in cc.review_bids_folder(tmp_path) if r.subject == "A"]

    cc.set_note(tmp_path, reviews, "  moved a lot ")

    assert cc.read_notes(tmp_path) == {
        "20241014_1341_A": "moved a lot",
        "20241015_0900_A": "moved a lot",
    }
    assert review_of(tmp_path, "sub-A_ses-2").note == "moved a lot"
    assert [a["action"] for a in read_actions(tmp_path)] == ["note", "note"]
    cc.set_note(tmp_path, reviews[:1], "")
    assert review_of(tmp_path, "sub-A_ses-1").note == ""


def test_delete_session_excludes_its_dicom_folder(tmp_path: Path) -> None:
    two_sessions(tmp_path)
    cc.delete_session(tmp_path, review_of(tmp_path, "sub-A_ses-1"), "aborted visit")

    assert not (tmp_path / "sub-A" / "ses-1").exists()
    assert read_excluded_sessions(tmp_path) == {"20241014_1341_A": "aborted visit"}
    [logged] = read_actions(tmp_path)
    assert (logged["action"], logged["target"]) == ("delete session", "sub-A_ses-1")
    assert "from 20241014_1341_A" in logged["detail"]


def test_delete_session_refused_without_its_source_folder(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Rest_bold.nii.gz"), 495)
    with pytest.raises(cc.CrosscheckError, match="Convert / update BIDS first"):
        cc.delete_session(tmp_path, review_of(tmp_path, "sub-A_ses-1"))
    assert (tmp_path / "sub-A" / "ses-1").is_dir()


def test_keep_run_and_delete_are_logged(tmp_path: Path) -> None:
    for run in (1, 2):
        write_nifti(func(tmp_path, "A", "1", f"task-Peer_run-{run}_bold.nii.gz"), 140)
    review = review_of(tmp_path, "sub-A_ses-1")
    cc.keep_run(tmp_path, review.pending[0], review.pending)

    actions = [(a["action"], a["target"]) for a in read_actions(tmp_path)]
    assert actions == [
        ("keep run", "sub-A/ses-1/func/sub-A_ses-1_task-Peer_run-1_bold.nii.gz"),
        ("delete file", "sub-A/ses-1/func/sub-A_ses-1_task-Peer_run-2_bold.nii.gz"),
    ]


def test_overview_table_flags_cells_like_the_old_bids_viewer(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Rest_bold.nii.gz"), 495)
    write_nifti(func(tmp_path, "A", "1", "task-Matching_bold.nii.gz"), 300)
    for run in (1, 2):
        write_nifti(func(tmp_path, "B", "1", f"task-Rest_run-{run}_bold.nii.gz"), 495)
    anat = tmp_path / "sub-A" / "ses-1" / "anat" / "sub-A_ses-1_T1w.nii.gz"
    write_nifti(anat, 1, tr=None)
    reviews = cc.review_bids_folder(tmp_path)

    tasks = cc.overview_tasks(reviews)
    assert tasks == ["Matching", "Rest"]  # the scan rules' order
    assert cc.overview_columns(tasks) == [
        "ID", "Session", "Date", "T1w", "Defaced",
        "Matching runs", "Matching volumes", "Matching events", "Rest runs", "Rest volumes",
    ]  # fmt: skip
    a, b = (cc.overview_row(r, tasks) for r in reviews)
    assert [(c.text, c.status) for c in a[3:]] == [
        ("1", ""), ("no", "check"),  # T1w present, not defaced
        ("1", ""), ("300", "short"), ("-", "missing"),  # Matching short, no events
        ("1", ""), ("495", ""),
    ]  # fmt: skip
    assert [(c.text, c.status) for c in b[3:]] == [
        ("0", "missing"), ("-", ""),
        ("0", "missing"), ("-", ""), ("-", ""),
        ("2", "pending"), ("495, 495", ""),
    ]  # fmt: skip


def test_overview_csv_has_a_row_per_session_with_problems_and_note(tmp_path: Path) -> None:
    import csv

    two_sessions(tmp_path)
    cc.write_expected_sessions(tmp_path, 2)
    cc.set_note(tmp_path, [review_of(tmp_path, "sub-A_ses-2")], "moved a lot")
    out = tmp_path / "out.csv"

    cc.write_overview_csv(cc.review_bids_folder(tmp_path), out)

    with out.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [(r["ID"], r["Session"], r["Rest volumes"], r["Note"]) for r in rows] == [
        ("A", "ses-1", "495", ""),
        ("A", "ses-2", "495", "moved a lot"),
    ]
    assert rows[0]["Source folder"] == "20241014_1341_A"
    assert rows[0]["Problems"] == ""


def test_scan_lengths_save_themselves_once_typing_pauses(tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from fmri_local_process.gui.scan_lengths import AUTOSAVE_DELAY_MS, ScanLengthsPanel

    _app = QApplication.instance() or QApplication([])
    for subject in "AB":
        write_nifti(func(tmp_path, subject, "1", "task-Rest_bold.nii.gz"), 495)
    panel = ScanLengthsPanel(tmp_path, cc.find_data_files(tmp_path), None)
    saved: list[bool] = []
    panel.saved.connect(lambda: saved.append(True))

    panel.spins["task-Rest_bold"].setValue(4)  # typing 441: three changes...
    panel.spins["task-Rest_bold"].setValue(44)
    panel.spins["task-Rest_bold"].setValue(441)
    assert cc.read_expected(tmp_path) == {} and panel.saved_label.text() == "Saving..."
    QTest.qWait(AUTOSAVE_DELAY_MS + 300)

    assert cc.read_expected(tmp_path) == {"task-Rest_bold": 441}  # ...saved once
    assert saved == [True] and panel.saved_label.text() == "Lengths saved"
    assert [a["detail"] for a in read_actions(tmp_path)] == ["automatic -> 441"]
