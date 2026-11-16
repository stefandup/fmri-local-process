"""Save / restore of the crosscheck decisions, and replaying them onto a fresh conversion."""

import json
from datetime import date
from pathlib import Path

from test_crosscheck import func, review_of, write_bold, write_nifti

from fmri_local_process.processing import crosscheck as cc
from fmri_local_process.processing import crosscheck_backup as cb
from fmri_local_process.processing.philani_bids import (
    DICOM_CORRECTIONS_FILE,
    exclude_session,
    write_dicom_corrections,
)


def sessions_tsv(bids: Path, subject: str, folder: str) -> None:
    (bids / f"sub-{subject}" / f"sub-{subject}_sessions.tsv").write_text(
        f"session_id\tacq_time\tsource_folder\nses-1\t2024-10-14T13:41:00\t{folder}\n"
    )


def test_backup_keeps_latest_and_a_dated_copy_and_restores(tmp_path: Path) -> None:
    bids, backups = tmp_path / "bids", tmp_path / "backups"
    write_dicom_corrections(bids, {"20241014_1341_12345 2": "12345-2"})
    exclude_session(bids, "20240101_X_P009", "dud")

    saved = cb.save_backup(bids, backups, today=date(2026, 11, 16))

    assert DICOM_CORRECTIONS_FILE in saved
    assert [b.name for b in cb.list_backups(backups)] == ["latest", "2026-11-16"]
    fresh = tmp_path / "fresh_bids"
    restored = cb.restore_backup(backups / "latest", fresh)
    assert set(restored) == set(saved)
    assert json.loads((fresh / DICOM_CORRECTIONS_FILE).read_text()) == {
        "20241014_1341_12345 2": "12345-2"
    }


def test_a_bids_folder_that_lost_its_records_never_wipes_the_backup(tmp_path: Path) -> None:
    bids, backups = tmp_path / "bids", tmp_path / "backups"
    write_dicom_corrections(bids, {"20241014_1341_A": "A"})
    cb.save_backup(bids, backups)

    (bids / DICOM_CORRECTIONS_FILE).unlink()  # e.g. the BIDS folder was partly lost
    cb.save_backup(bids, backups)  # auto-save runs anyway (the action log is still there)
    saved = backups / "latest" / DICOM_CORRECTIONS_FILE
    assert json.loads(saved.read_text()) == {"20241014_1341_A": "A"}  # still backed up

    write_dicom_corrections(bids, {})  # clearing in the tool: an empty record, saved as such
    cb.save_backup(bids, backups)
    assert json.loads(saved.read_text()) == {}

    empty = tmp_path / "empty_bids"
    assert cb.save_backup(empty, backups) == []  # no records at all: nothing touched


def test_kept_runs_and_trims_replay_after_reconversion(tmp_path: Path) -> None:
    bids = tmp_path
    for run in (1, 2):
        write_nifti(func(bids, "A", "1", f"task-Peer_run-{run}_bold.nii.gz"), 140)
    write_bold(func(bids, "A", "1", "task-Rest_bold.nii.gz"), 10)
    sessions_tsv(bids, "A", "20241014_1341_A")
    review = review_of(bids, "sub-A_ses-1")
    cc.keep_run(bids, review.pending[1], review.pending)  # run-2
    rest = next(d for d in cc.find_data_files(bids) if d.label == "task-Rest_bold")
    cc.trim_run(bids, rest, 6)
    decisions = cc.read_decisions(bids)
    assert decisions[cc.KEPT_RUNS] == {"20241014_1341_A": {"task-Peer_bold": "run-2"}}
    assert decisions[cc.TRIMS] == {"20241014_1341_A": {"task-Rest_bold": 6}}

    # A fresh conversion (here: the files as the converter would write them again).
    (func(bids, "A", "1", "task-Peer_bold.nii.gz")).unlink()
    for run in (1, 2):
        write_nifti(func(bids, "A", "1", f"task-Peer_run-{run}_bold.nii.gz"), 140)
    write_bold(func(bids, "A", "1", "task-Rest_bold.nii.gz"), 10)

    applied, unresolved = cc.replay_decisions(bids)

    assert unresolved == []
    assert applied == [
        "sub-A_ses-1: kept run-2 of task-Peer_bold",
        "sub-A_ses-1_task-Rest_bold.nii.gz: trimmed to 6 volumes",
    ]
    assert func(bids, "A", "1", "task-Peer_bold.nii.gz").exists()
    assert cc.replay_decisions(bids) == ([], [])  # replaying again changes nothing


def test_replay_reports_what_it_cannot_find(tmp_path: Path) -> None:
    bids = tmp_path
    (bids / cc.DECISIONS_FILE).parent.mkdir(parents=True)
    (bids / cc.DECISIONS_FILE).write_text(
        json.dumps({cc.KEPT_RUNS: {"20240101_X_GONE": {"task-Rest_bold": "run-1"}}})
    )
    applied, unresolved = cc.replay_decisions(bids)
    assert applied == [] and "20240101_X_GONE" in unresolved[0]


def test_crosscheck_mark_follows_the_dicom_folder_and_is_logged(tmp_path: Path) -> None:
    write_nifti(func(tmp_path, "A", "1", "task-Rest_bold.nii.gz"), 300)  # short: an issue
    sessions_tsv(tmp_path, "A", "20241014_1341_A")
    review = review_of(tmp_path, "sub-A_ses-1")
    assert review.issues and not review.crosschecked

    cc.set_crosschecked(tmp_path, [review], True)

    assert review_of(tmp_path, "sub-A_ses-1").crosschecked
    assert "20241014_1341_A" in cc.read_decisions(tmp_path)[cc.CROSSCHECKED]
    cc.set_crosschecked(tmp_path, [review_of(tmp_path, "sub-A_ses-1")], False)
    assert not review_of(tmp_path, "sub-A_ses-1").crosschecked
    from fmri_local_process.processing.action_log import read_actions

    assert [a["action"] for a in read_actions(tmp_path)] == [
        "mark crosschecked",
        "un-mark crosschecked",
    ]
