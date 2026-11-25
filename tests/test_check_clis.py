"""The command-line tools are enough to check and crosscheck without any window or terminal app: on
the dummy data's made-up participants, from raw to a BIDS folder with every decision recorded."""

from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from test_crosscheck import func, write_bold
from test_mriqc import make_bids as make_mriqc_bids
from test_mriqc import write_iqms

from fmri_local_process.app import backup
from fmri_local_process.app import mriqc as am
from fmri_local_process.app.behav import BehavWork, load_behav_data
from fmri_local_process.cli import (
    behav_check,
    behav_fix,
    bids_check,
    bids_fix,
    convert_to_bids,
    folders,
    mriqc,
    preprocess,
)
from fmri_local_process.processing import behav_match as bm
from fmri_local_process.processing import crosscheck as cc
from fmri_local_process.processing import mriqc as mq
from fmri_local_process.processing import philani_bids as pb
from fmri_local_process.processing.dummy_data import generate_dummy_dataset


def run(command, *args: object, code: int = 0, answer: str = "") -> Result:  # noqa: ANN001 -- click
    result = CliRunner().invoke(command, [str(a) for a in args], input=answer)
    assert result.exit_code == code, f"{result.output}\n{result.exception!r}"
    return result


@pytest.fixture
def study(tmp_path: Path) -> tuple[Path, Path, Path]:
    generate_dummy_dataset(tmp_path / "dummy", 1, with_errors=True, seed=1)
    made = tmp_path / "bids", tmp_path / "dummy" / "behav", tmp_path / "dummy" / "dicom"
    for key, folder in zip(("--bids", "--behav", "--dicom"), made, strict=True):
        run(folders.main, key, folder, "--yes")
    return made


def status_of(study: tuple[Path, Path, Path], subject: str, task: str = "Matching") -> list[str]:
    bids, behav, dicom = study
    found = behav_check.find_matches(behav, dicom, bids)
    return [
        m.status
        for m in found
        if m.info.subject == subject and m.info.task is not None and m.info.task.task == task
    ]


def key_of(study: tuple[Path, Path, Path], text: str) -> str:
    _bids, behav, _dicom = study
    logs = sorted(p.relative_to(behav).as_posix() for p in (behav / "emolabeling").rglob("*.log"))
    return next(key for key in logs if text in key)


# ---- logs and DICOM folders ---------------------------------------------------------------------


def test_the_list_is_the_tabs_list(study: tuple[Path, Path, Path], tmp_path: Path) -> None:
    bids, behav, dicom = study
    out = run(behav_check.main, "--csv", tmp_path / "logs.csv")
    work = BehavWork(bids, behav, dicom, load_behav_data(bids, behav, dicom))
    matched = sum(m.status == bm.MATCHED for m in work.matches)
    assert f"{matched} of {len(work.matches)} log(s) matched to a session" in out.output
    assert f"{work.waiting()} need review" in out.output
    assert len((tmp_path / "logs.csv").read_text().splitlines()) == len(work.matches) + 1
    few = run(behav_check.main, "--problems-only").output
    assert "DUMMY000" not in few and "DUMMY0033" in few


def test_a_typo_is_corrected_and_applied(study: tuple[Path, Path, Path]) -> None:
    bids, behav, dicom = study
    key = key_of(study, "DUMMY0033")
    assert status_of(study, "DUMMY0033") == [bm.UNKNOWN_ID]
    run(behav_fix.main, "log", key, "--id", "DUMMY003", "--crosschecked")
    assert status_of(study, "DUMMY003") == [bm.MATCHED]
    assert "1 crosschecked" in run(behav_check.main).output
    run(behav_fix.main, "log", key, "--id", "", "--not-crosschecked")
    assert status_of(study, "DUMMY0033") == [bm.UNKNOWN_ID] and bm.read_corrections(bids) == {}


def test_a_folder_without_a_date_gets_one(study: tuple[Path, Path, Path]) -> None:
    bids = study[0]
    folder, args = "visit one DUMMY015", ("--id", "DUMMY015", "--date", "2002-06-24")
    run(behav_fix.main, "folder", folder, *args, "--session", "2", "--crosschecked")
    assert status_of(study, "DUMMY015") == [bm.MATCHED]
    assert pb.read_dicom_session_corrections(bids) == {folder: "2"}
    assert pb.read_crosschecked_folders(bids) == {folder}
    wrong = run(behav_fix.main, "folder", folder, "--date", "2002-02-30", code=1)
    assert "not a date" in wrong.output


def test_one_of_two_logs_is_used_and_a_bad_one_left_out(study: tuple[Path, Path, Path]) -> None:
    bids, behav, dicom = study
    key = key_of(study, "DUMMY006")
    assert status_of(study, "DUMMY006") == [bm.DUPLICATE] * 2
    run(behav_fix.main, "use", key)
    assert sorted(status_of(study, "DUMMY006")) == sorted([bm.MATCHED, bm.EXCLUDED])
    again = run(behav_fix.main, "use", key_of(study, "DUMMY000"), code=1)
    assert "no other log" in again.output
    aborted = key_of(study, "DUMMY007")
    run(behav_fix.main, "log", aborted, "--verdict", "error", "--note", "stopped")
    assert status_of(study, "DUMMY007") == [bm.EXCLUDED]
    assert bm.read_corrections(bids)[aborted].note == "stopped"
    run(behav_fix.main, "log", aborted, "--verdict", "none")
    assert status_of(study, "DUMMY007") == [bm.MATCHED]
    lonely = run(behav_fix.main, "log", aborted, "--note", "why", code=1)
    assert "goes with a verdict" in lonely.output


def test_one_logs_events_are_shown(study: tuple[Path, Path, Path]) -> None:
    bids, behav, dicom = study
    out = run(behav_check.main, "--log", key_of(study, "DUMMY000"))
    assert "events end at" in out.output and "block_fixation" in out.output
    assert "isn't in the BIDS folder yet" in out.output
    bad = run(behav_check.main, "--log", key_of(study, "DUMMY008"), code=1)
    assert "Can't extract events" in bad.output
    assert "No such log" in run(behav_check.main, "--log", "x", code=1).output


def test_a_part_of_a_logs_name_is_enough(study: tuple[Path, Path, Path]) -> None:
    run(behav_fix.main, "log", "DUMMY0033-emo", "--id", "DUMMY003")
    assert status_of(study, "DUMMY003") == [bm.MATCHED]
    several = run(behav_fix.main, "log", "DUMMY006-emo", "--verdict", "ok", code=1)
    assert "2 logs have" in several.output


# ---- which folders ------------------------------------------------------------------------------


def test_folders_are_set_once_and_shown(tmp_path: Path) -> None:
    raw, bids = tmp_path / "raw", tmp_path / "bids"
    raw.mkdir()
    assert "fmri_folders --bids" in run(bids_check.main, code=2).output  # says how
    run(folders.main, "--dicom", raw, "--bids", bids, code=1, answer="n\n")
    out = run(folders.main, "--dicom", raw, "--bids", bids, answer="y\n").output
    assert str(raw) in out and bids.is_dir()
    assert "its own output" in run(folders.main, "--dicom", bids / "x", code=1).output
    assert "no folder" in run(folders.main, "--behav", tmp_path / "typo", code=1).output
    assert str(raw) not in run(folders.main, "--clear").output


def test_folders_of_two_studies_are_never_mixed(study: tuple[Path, Path, Path]) -> None:
    """With some folders given, the remembered ones stay out of it."""
    _bids, behav, dicom = study
    out = run(behav_check.main, behav, code=2).output
    assert "DICOM sessions" in out and "or none" in out
    assert "BIDS" in run(behav_fix.main, "log", "x", "--behav", behav, code=2).output
    run(behav_fix.main, "log", "DUMMY0033-emo", "--id", "DUMMY003")
    assert "26 of 38" in run(behav_check.main).output
    assert "25 of 38" in run(behav_check.main, behav, dicom).output  # no --bids: no corrections


def test_long_jobs_ask_before_using_the_remembered_folders(
    study: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    bids = study[0]
    out = run(convert_to_bids.main, code=1, answer="n\n").output
    assert str(bids) in out and "Convert these?" in out and not any(bids.glob("sub-*"))
    run(folders.main, "--output", tmp_path / "cpac", "--yes")
    assert "Preprocess this?" in run(preprocess.main, code=1, answer="n\n").output


# ---- the BIDS folder ----------------------------------------------------------------------------


@pytest.fixture
def bids(tmp_path: Path) -> Path:
    """sub-A with two sessions: a Rest run each, ses-2's too long and one scan in two runs."""
    root = tmp_path / "bids"
    for session, volumes in (("1", 10), ("2", 14)):
        write_bold(func(root, "A", session, "task-Rest_bold.nii.gz"), volumes)
    for n in (1, 2):
        write_bold(func(root, "A", "2", f"task-Peer_run-{n}_bold.nii.gz"), 10)
    (root / "sub-A" / "sub-A_sessions.tsv").write_text(
        "session_id\tacq_time\tsource_folder\n"
        "ses-1\t2024-10-14T13:41:00\t20241014_1341_A\n"
        "ses-2\t2024-10-15T09:00:00\t20241015_0900_A\n"
    )
    run(folders.main, "--bids", root)
    return root


def test_bids_check_shows_what_the_crosscheck_shows(bids: Path, tmp_path: Path) -> None:
    csv = tmp_path / "o.csv"
    out = run(bids_check.main, "--all", "--files", "--lengths", "--csv", csv).output
    assert "1 subject(s), 2 session(s)" in out
    assert "keep one: sub-A/ses-2/func/sub-A_ses-2_task-Peer_run-2_bold.nii.gz" in out
    assert "task-Rest_bold" in out and csv.is_file()
    for review in cc.review_bids_folder(bids):
        assert all(issue.text.split(":")[0] in out for issue in review.issues)


def test_every_crosscheck_decision_can_be_recorded(
    bids: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup, "backup_base", lambda: tmp_path / "backups")
    fix = bids_fix.main
    run(fix, "mark", "sub-A_ses-1", "sub-A_ses-2")
    run(fix, "unmark", "sub-A_ses-2")
    run(fix, "note", "sub-A_ses-1", "moved [a lot]")
    first, second = (r for r in cc.review_bids_folder(bids) if r.session)
    assert (first.crosschecked, second.crosschecked, first.note) == (True, False, "moved [a lot]")
    assert "No such session" in run(fix, "mark", "sub-X", code=1).output

    chosen = bids / "sub-A/ses-2/func/sub-A_ses-2_task-Peer_run-2_bold.nii.gz"
    run(fix, "keep", chosen.name, code=1, answer="n\n")
    assert chosen.exists()  # asked first
    run(fix, "keep", chosen, answer="y\n")
    left = sorted(p.name for p in chosen.parent.glob("*Peer*.nii.gz"))
    assert left == ["sub-A_ses-2_task-Peer_bold.nii.gz"]

    run(fix, "expect", "task-Rest_bold", 10)
    run(fix, "trim", code=1, answer="n\n")
    out = run(fix, "trim", "--yes").output
    assert "to 10 volumes" in out
    assert "No run is too long" in run(fix, "trim").output
    run(fix, "expect", "task-Rest_bold", 0)
    assert cc.read_expected(bids) == {}
    run(fix, "sessions-per-subject", 2)
    assert cc.read_expected_sessions(bids) == 2

    assert "file(s) to" in run(fix, "backup").output
    assert (tmp_path / "backups" / "philani").is_dir()
    run(fix, "delete-file", chosen.with_name(left[0]), "--yes")
    run(fix, "delete-session", "sub-A_ses-2", "--yes")
    assert not (bids / "sub-A" / "ses-2").exists()
    assert "20241015_0900_A" in pb.read_excluded_sessions(bids)
    assert "delete" in run(bids_check.main, "--actions", 3).output


# ---- MRIQC --------------------------------------------------------------------------------------


def test_mriqc_runs_lists_and_rates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bids, output = tmp_path / "bids", tmp_path / "out"
    make_mriqc_bids(bids)
    run(folders.main, "--bids", bids, "--mriqc", output, "--yes")
    ran: list[tuple[str, int, float]] = []

    def run_subject(*args: object, **_kw: object) -> str | None:
        ran.append((str(args[3]), int(str(args[4])), float(str(args[5]))))
        return None if args[3] == "01" else "out of memory"

    monkeypatch.setattr(mq, "docker_status", lambda: (mq.READY, "", Path("docker")))
    monkeypatch.setattr(mriqc, "docker_resources", lambda _docker: (32.0, 8))
    monkeypatch.setattr(mq, "run_subject", run_subject)
    monkeypatch.setattr(mq, "run_logged", lambda *_: 0)
    out = run(mriqc.main, code=1).output  # one failed
    assert ran == [("01", 8, 30.0), ("02", 8, 30.0)] and "FAILED: out of memory" in out
    write_iqms(output, "sub-01/ses-1/func/sub-01_ses-1_task-rest_bold.nii.gz", fd_mean=0.2)
    name = "sub-01_ses-1_task-rest_bold"
    run(mriqc.main, "--rate", name, "1", "--comment", "spikes")
    rating = mq.read_ratings(output)[name]
    assert (rating.rating, rating.comment) == (1, "spikes")
    assert am.MODALITY_TEXT["bold"] in run(mriqc.main, "--results").output
    assert "No such subject" in run(mriqc.main, "--subject", "9", code=1).output


def test_mriqc_says_when_docker_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_mriqc_bids(tmp_path / "bids")
    monkeypatch.setattr(mq, "docker_status", lambda: (mq.DOCKER_MISSING, "no docker here", None))
    out = run(mriqc.main, tmp_path / "bids", tmp_path / "out", code=1).output
    assert "no docker here" in out
