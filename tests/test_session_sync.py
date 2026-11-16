"""Keeping C-PAC output in step with renumbered BIDS sessions (synthetic folders only)."""

from pathlib import Path

import pytest

from fmri_local_process.processing import session_sync as sync
from fmri_local_process.processing.cpac_docker import is_subject_done

HEADER = "session_id\tacq_time\tsource_folder\n"


def write_sessions(path: Path, *rows: tuple[str, str]) -> None:
    """rows: (label, DICOM folder)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "".join(f"ses-{label}\t2024-01-01\t{f}\n" for label, f in rows))


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def output_for(out: Path, subject: str, session: str) -> Path:
    func = out / "output" / "pipeline_cpac" / f"sub-{subject}" / f"ses-{session}" / "func"
    return touch(func / f"sub-{subject}_ses-{session}_task-Rest_desc-preproc_bold.nii.gz")


@pytest.fixture
def renumbered(tmp_path: Path) -> tuple[Path, Path]:
    """Output made with ses-1/ses-2; then an earlier scan made them ses-2/ses-3 in BIDS."""
    bids, out = tmp_path / "bids", tmp_path / "out"
    write_sessions(bids / "sub-A" / "sub-A_sessions.tsv", ("1", "0301_A"), ("2", "0401_A"))
    for session in "12":
        output_for(out, "A", session)
        touch(out / "log" / "pipeline_cpac" / f"sub-A_ses-{session}" / "expectedOutputs.yml")
    output_for(out, "A1", "1")  # another subject whose ID starts the same: untouched
    output_for(out, "BA", "1")  # ... or ends the same
    sync.adopt_sessions(bids, out, "A")  # output from before sessions were recorded
    write_sessions(
        bids / "sub-A" / "sub-A_sessions.tsv", ("1", "0101_A"), ("2", "0301_A"), ("3", "0401_A")
    )
    return bids, out


def test_change_found_after_renumbering(renumbered: tuple[Path, Path]) -> None:
    bids, out = renumbered
    changes = sync.session_changes(bids, out, "A")
    assert [str(c) for c in changes] == ["ses-1 -> ses-2 (0301_A)", "ses-2 -> ses-3 (0401_A)"]
    assert sync.session_changes(bids, out, "A1") == []  # no record: nothing to compare


def test_renumbered_subject_never_counts_as_done(renumbered: tuple[Path, Path]) -> None:
    bids, out = renumbered
    for session in "123":
        touch(
            bids
            / "sub-A"
            / f"ses-{session}"
            / "func"
            / f"sub-A_ses-{session}_task-Rest_bold.nii.gz"
        )
    output_for(out, "A", "3")  # every label now has output, but from the wrong scans
    assert not is_subject_done(bids, out, "A", None)


def test_rename_outputs_moves_old_labels_to_new_ones(renumbered: tuple[Path, Path]) -> None:
    bids, out = renumbered
    changes = sync.session_changes(bids, out, "A")

    renamed = sync.rename_outputs(bids, out, "A", changes)

    pipeline = out / "output" / "pipeline_cpac"
    assert sorted(p.name for p in (pipeline / "sub-A").iterdir()) == ["ses-2", "ses-3"]
    assert (
        pipeline / "sub-A" / "ses-3" / "func" / "sub-A_ses-3_task-Rest_desc-preproc_bold.nii.gz"
    ).exists()
    assert sorted(p.name for p in (out / "log" / "pipeline_cpac").iterdir()) == [
        "sub-A_ses-2",
        "sub-A_ses-3",
    ]
    assert (
        pipeline / "sub-A1" / "ses-1" / "func" / "sub-A1_ses-1_task-Rest_desc-preproc_bold.nii.gz"
    ).exists()
    assert (
        pipeline / "sub-BA" / "ses-1" / "func" / "sub-BA_ses-1_task-Rest_desc-preproc_bold.nii.gz"
    ).exists()
    assert len(renamed) == 6  # 2 session folders, 2 files, 2 log folders
    assert sync.session_changes(bids, out, "A") == []  # recorded: in step again
