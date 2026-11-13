"""Tests for processing/cpac_docker.py on a synthetic BIDS tree (no Docker needed)."""

from pathlib import Path

from fmri_local_process.processing.cpac_docker import (
    CPAC_IMAGE,
    bids_subjects,
    bold_sessions,
    build_cpac_command,
    is_preprocessed,
)


def make_bids(root: Path) -> Path:
    (root / "dataset_description.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "dataset_description.json").write_text("{}")
    for rel in [
        "sub-P01/ses-1/func/sub-P01_ses-1_task-rest_bold.nii.gz",
        "sub-P01/ses-2/func/sub-P01_ses-2_task-rest_bold.nii.gz",
        "sub-P02/ses-1/anat/sub-P02_ses-1_T1w.nii.gz",  # no BOLD -> not a subject to run
        "crosscheck_pending/sub-P03_ses-1_task-rest_run-2_bold.nii.gz",
    ]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).touch()
    return root


def test_subjects_and_sessions(tmp_path: Path) -> None:
    bids = make_bids(tmp_path / "bids")
    assert bids_subjects(bids) == ["P01"]
    assert bold_sessions(bids, "P01") == ["1", "2"]


def test_task_filter(tmp_path: Path) -> None:
    bids = make_bids(tmp_path / "bids")
    matching = bids / "sub-P01/ses-2/func/sub-P01_ses-2_task-Matching_run-2_bold.nii.gz"
    matching.touch()
    assert bids_subjects(bids, "Matching") == ["P01"]
    assert bold_sessions(bids, "P01", "Matching") == ["2"]
    assert bids_subjects(bids, "Match") == []  # whole task label only

    out = tmp_path / "out"
    func = out / "output" / "pipeline_cpac_preproc" / "sub-P01" / "ses-2" / "func"
    func.mkdir(parents=True)
    (func / "sub-P01_ses-2_task-rest_space-template_desc-preproc_bold.nii.gz").touch()
    assert not is_preprocessed(out, "P01", ["2"], "Matching")
    (func / "sub-P01_ses-2_task-Matching_run-2_space-template_desc-preproc_bold.nii.gz").touch()
    assert is_preprocessed(out, "P01", ["2"], "Matching")

    cmd = build_cpac_command(Path("docker"), bids, out, "P01", CPAC_IMAGE, 1, 3.0, "Matching")
    assert cmd[-2:] == ["--bold_label", "task-Matching"]
    assert "--bold_label" not in build_cpac_command(
        Path("docker"), bids, out, "P01", CPAC_IMAGE, 1, 3.0
    )


def test_is_preprocessed_needs_every_session(tmp_path: Path) -> None:
    out = tmp_path / "out"
    func = out / "output" / "pipeline_cpac_preproc" / "sub-P01" / "ses-1" / "func"
    func.mkdir(parents=True)
    (func / "sub-P01_ses-1_task-rest_space-template_desc-preproc_bold.nii.gz").touch()
    assert is_preprocessed(out, "P01", ["1"])
    assert not is_preprocessed(out, "P01", ["1", "2"])
    assert not is_preprocessed(out, "P0", ["1"])  # no prefix matches
    assert not is_preprocessed(out, "P01", [])


def test_command_mounts_only_the_subject(tmp_path: Path) -> None:
    bids = make_bids(tmp_path / "bids")
    out = tmp_path / "out"
    cmd = build_cpac_command(Path("docker"), bids, out, "P01", CPAC_IMAGE, 4, 8.0)
    mounts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-v"]
    description = (bids / "dataset_description.json").resolve()
    assert mounts == [
        f"{(bids / 'sub-P01').resolve()}:/bids_dataset/sub-P01:ro",
        f"{description}:/bids_dataset/dataset_description.json:ro",
        f"{out.resolve()}:/outputs",
    ]
    tail = cmd[cmd.index(CPAC_IMAGE) :]
    assert tail[:4] == [CPAC_IMAGE, "/bids_dataset", "/outputs", "participant"]
    assert tail[tail.index("--preconfig") + 1] == "preproc"
    assert tail[tail.index("--participant_label") + 1] == "P01"
    assert tail[tail.index("--n_cpus") + 1] == "4"
