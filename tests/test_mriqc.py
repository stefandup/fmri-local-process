"""MRIQC: the docker commands, done / stale per subject, reading IQMs and flagging outliers,
on a synthetic BIDS folder and synthetic MRIQC outputs (no Docker, no real data)."""

import json
import os
from pathlib import Path

import pytest

from fmri_local_process.processing import mriqc as mq

DOCKER = Path("docker")


def make_bids(bids: Path) -> None:
    """sub-01 with two sessions (a BOLD each, a T1w in ses-1); sub-02 with one BOLD, no T1w."""
    (bids / "dataset_description.json").parent.mkdir(parents=True, exist_ok=True)
    (bids / "dataset_description.json").write_text("{}")
    files = [
        "sub-01/ses-1/anat/sub-01_ses-1_T1w.nii.gz",
        "sub-01/ses-1/func/sub-01_ses-1_task-rest_bold.nii.gz",
        "sub-01/ses-2/func/sub-01_ses-2_task-rest_bold.nii.gz",
        "sub-02/ses-1/func/sub-02_ses-1_task-rest_bold.nii.gz",
        "sub-02/ses-1/func/sub-02_ses-1_task-rest_sbref.nii.gz",  # not a modality MRIQC gets
    ]
    for name in files:
        (bids / name).parent.mkdir(parents=True, exist_ok=True)
        (bids / name).write_bytes(b"")
    (bids / "crosscheck_pending").mkdir()


def write_iqms(output: Path, relative: str, **values: float) -> Path:
    """An MRIQC-style IQM file (numbers plus the metadata blocks it also writes)."""
    path = output / relative.replace(".nii.gz", ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**values, "bids_meta": {"x": 1}, "provenance": {"v": "24"}}))
    return path


def test_scans_subjects_and_modalities(tmp_path: Path) -> None:
    make_bids(tmp_path)
    names = [scan.name for scan in mq.bids_scans(tmp_path)]
    assert names == [
        "sub-01_ses-1_T1w",
        "sub-01_ses-1_task-rest_bold",
        "sub-01_ses-2_task-rest_bold",
        "sub-02_ses-1_task-rest_bold",
    ]
    assert mq.bids_subjects(tmp_path) == ["01", "02"]
    assert mq.subject_modalities(tmp_path, "01") == ["bold", "T1w"]
    assert mq.subject_modalities(tmp_path, "02") == ["bold"]  # only asked for what it has


def test_participant_command_mounts_only_the_subject(tmp_path: Path) -> None:
    make_bids(tmp_path / "bids")
    cmd = mq.build_participant_command(
        DOCKER, tmp_path / "bids", tmp_path / "out", "02", ["bold"], 6, 12.0
    )
    mounts = [cmd[i + 1] for i, part in enumerate(cmd) if part == "-v"]
    assert any(m.endswith(":/data/sub-02:ro") for m in mounts)
    assert any(m.endswith(":/data/dataset_description.json:ro") for m in mounts)
    assert not any("crosscheck_pending" in m for m in mounts)
    tail = cmd[cmd.index(mq.MRIQC_IMAGE) :]
    assert tail[:4] == [mq.MRIQC_IMAGE, "/data", "/out", "participant"]
    assert ["--participant-label", "02"] == tail[4:6]
    assert ["-m", "bold", "--nprocs", "6", "--mem_gb", "12"] == tail[6:12]
    assert "--no-sub" in tail and "--notrack" in tail  # nothing leaves the computer
    group = mq.build_group_command(DOCKER, tmp_path / "bids", tmp_path / "out")
    assert "group" in group and "--no-sub" in group


def test_selection_picks_scans_and_tasks(tmp_path: Path) -> None:
    make_bids(tmp_path)
    extra = tmp_path / "sub-02/ses-1/func/sub-02_ses-1_task-faces_bold.nii.gz"
    extra.write_bytes(b"")
    assert mq.bids_tasks(tmp_path) == ["faces", "rest"] and mq.has_t1w(tmp_path)
    faces = mq.Selection(t1w=False, tasks=("faces",))
    assert [s.name for s in mq.bids_scans(tmp_path, selection=faces)] == [
        "sub-02_ses-1_task-faces_bold"
    ]
    assert mq.bids_subjects(tmp_path, faces) == ["02"]
    assert mq.bids_subjects(tmp_path, mq.Selection(tasks=())) == ["01"]  # T1w only
    assert mq.Selection(t1w=False, tasks=()).empty
    cmd = mq.build_participant_command(
        DOCKER, tmp_path, tmp_path / "out", "02", ["bold"], 2, 8.0, tasks=("faces",)
    )
    assert cmd[cmd.index("--task-id") + 1] == "faces"
    assert "-v" in cmd  # MRIQC logs each finished step: the progress bar counts them


def test_subject_state_done_partial_stale(tmp_path: Path) -> None:
    bids, out = tmp_path / "bids", tmp_path / "out"
    make_bids(bids)
    assert mq.subject_state(bids, out, "01") == mq.NOT_RUN
    write_iqms(out, "sub-01/ses-1/func/sub-01_ses-1_task-rest_bold.nii.gz", tsnr=50)
    assert mq.subject_state(bids, out, "01") == mq.PARTIAL
    write_iqms(out, "sub-01/ses-2/func/sub-01_ses-2_task-rest_bold.nii.gz", tsnr=50)
    t1w = write_iqms(out, "sub-01/ses-1/anat/sub-01_ses-1_T1w.nii.gz", cnr=3)
    assert mq.subject_state(bids, out, "01") == mq.DONE
    # The BIDS file changed after MRIQC read it (trimmed): stale.
    earlier = t1w.stat().st_mtime - 100
    os.utime(t1w, (earlier, earlier))
    assert mq.subject_state(bids, out, "01") == mq.STALE
    assert mq.has_results(out) and not mq.has_results(tmp_path / "empty")


def test_collect_iqms_reads_numbers_and_groups_by_subject_session(tmp_path: Path) -> None:
    out = tmp_path / "out"
    for subject, session in (("02", "1"), ("01", "2"), ("01", "1")):
        name = f"sub-{subject}_ses-{session}_task-rest_bold"
        write_iqms(out, f"sub-{subject}/ses-{session}/func/{name}.nii.gz", tsnr=40, fd_mean=0.1)
        (out / f"{name}.html").write_text("<html></html>")
    bad = out / "sub-03/ses-1/func/sub-03_ses-1_task-rest_bold.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("not json")

    runs, unreadable = mq.collect_iqms(out, "bold")
    assert len(runs) == 3 and len(unreadable) == 1
    assert runs[0].values == {"tsnr": 40.0, "fd_mean": 0.1}  # metadata blocks left out
    assert runs[0].report is not None and runs[0].label == "task-rest_bold"
    grouped = mq.by_subject_session(runs)
    assert list(grouped) == ["01", "02"]
    assert list(grouped["01"]) == ["1", "2"]
    assert mq.collect_iqms(out, "T1w") == ([], [])


def test_outliers_only_on_the_worse_side_and_within_the_same_task(tmp_path: Path) -> None:
    assert mq.outlier_flags([1, 1, 1, 1], "lower") == [False] * 4  # too few to judge
    values = [0.10, 0.11, 0.12, 0.10, 0.11, 0.90, 0.01]
    assert mq.outlier_flags(values, "lower") == [False] * 5 + [True, False]
    assert mq.outlier_flags(values, "higher") == [False] * 6 + [True]
    assert mq.outlier_flags(values, None) == [False] * 5 + [True, True]

    def run(subject: str, task: str, fd: float) -> mq.RunIqms:
        name = f"sub-{subject}_ses-1_task-{task}_run-1_bold"
        return mq.RunIqms(name, "bold", subject, {"fd_mean": fd})

    rest = [run(str(n), "rest", 0.1) for n in range(5)] + [run("9", "rest", 0.8)]
    # A task with high motion everywhere is its own comparison group: not flagged.
    task = [run(str(n), "faces", 0.8) for n in range(5)]
    flags = mq.flag_outliers(rest + task)
    assert flags["sub-9_ses-1_task-rest_run-1_bold"] == {"fd_mean"}
    assert sum(bool(f) for f in flags.values()) == 1


def test_explain_failure(tmp_path: Path) -> None:
    log = tmp_path / "log.txt"
    log.write_text("starting\nValueError: no BOLD found\n")
    assert mq.explain_failure(log, 1).startswith("no BOLD found")
    log.write_text("Killed\n")
    assert "memory" in mq.explain_failure(log, 137)


@pytest.mark.parametrize("subject", ["01"])
def test_run_subject_reports_missing_iqms(
    tmp_path: Path, subject: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bids, out = tmp_path / "bids", tmp_path / "out"
    make_bids(bids)
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], log_path: Path, on_line=None) -> int:  # noqa: ANN001
        commands.append(cmd)
        write_iqms(out, "sub-01/ses-1/func/sub-01_ses-1_task-rest_bold.nii.gz", tsnr=1)
        return 0

    monkeypatch.setattr(mq, "run_logged", fake_run)
    failure = mq.run_subject(DOCKER, bids, out, subject, 2, 8.0)
    assert failure is not None and "sub-01_ses-1_T1w" in failure
    assert commands and "T1w" in commands[0]
