"""The command-line tools point out on their own what the tabs show, on the dummy data's made-up
participants. They change nothing: fixing is done in the toolbox."""

from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from test_crosscheck import func, write_bold
from test_mriqc import make_bids as make_mriqc_bids
from test_mriqc import write_iqms

from fmri_local_process.app import mriqc as am
from fmri_local_process.app.behav import BehavWork, load_behav_data
from fmri_local_process.cli import behav_check, bids_check, mriqc
from fmri_local_process.processing import behav_match as bm
from fmri_local_process.processing import crosscheck as cc
from fmri_local_process.processing import mriqc as mq
from fmri_local_process.processing import philani_bids as pb
from fmri_local_process.processing.dummy_data import generate_dummy_dataset


def run(command, *args: object, code: int = 0) -> Result:  # noqa: ANN001 -- click's
    result = CliRunner().invoke(command, [str(a) for a in args])
    assert result.exit_code == code, f"{result.output}\n{result.exception!r}"
    return result


def contents(folder: Path) -> dict[str, bytes]:
    return {p.as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


@pytest.fixture
def study(tmp_path: Path) -> tuple[Path, Path, Path]:
    generate_dummy_dataset(tmp_path / "dummy", 1, with_errors=True, seed=1)
    (tmp_path / "bids").mkdir()
    return tmp_path / "bids", tmp_path / "dummy" / "behav", tmp_path / "dummy" / "dicom"


def key_of(behav: Path, text: str) -> str:
    logs = sorted(p.relative_to(behav).as_posix() for p in (behav / "emolabeling").rglob("*.log"))
    return next(key for key in logs if text in key)


# ---- logs and DICOM folders ---------------------------------------------------------------------


def test_the_list_is_the_tabs_list(study: tuple[Path, Path, Path], tmp_path: Path) -> None:
    bids, behav, dicom = study
    before = contents(tmp_path)
    args = (behav, dicom, "--bids", bids)
    out = run(behav_check.main, *args).output
    assert contents(tmp_path) == before  # nothing is changed
    work = BehavWork(bids, behav, dicom, load_behav_data(bids, behav, dicom))
    matched = sum(m.status == bm.MATCHED for m in work.matches)
    assert f"{matched} of {len(work.matches)} log(s) matched to a session" in out
    assert f"{work.waiting()} need review" in out
    run(behav_check.main, *args, "--csv", tmp_path / "logs.csv")
    assert len((tmp_path / "logs.csv").read_text().splitlines()) == len(work.matches) + 1
    # Without a BIDS folder: the same, as nothing is saved in this one.
    assert run(behav_check.main, behav, dicom).output == out


def test_every_problem_is_pointed_out(study: tuple[Path, Path, Path]) -> None:
    bids, behav, dicom = study
    out = run(behav_check.main, behav, dicom, "--bids", bids, "--problems-only").output
    one_line = " ".join(out.split())
    assert "DUMMY000" not in out  # the clean one
    for problem in (
        "unknown ID",  # log_id_typo
        "characters removed",  # log_id_space
        "date mismatch",
        "duplicate",
        "before the thank-you screen",
        "unreadable",
        "no Matching log",  # missing_log
        "no task scan",
        "no task",
        "columns (the R script expects",
        "not converted: no date or ID in the folder name",
    ):
        assert problem in one_line, problem


def test_saved_corrections_are_applied(study: tuple[Path, Path, Path]) -> None:
    """Those of logs and those of DICOM folders, as the tabs save them."""
    bids, behav, dicom = study
    args = (behav, dicom, "--bids", bids)
    assert "25 of 38" in run(behav_check.main, *args).output
    bm.write_corrections(bids, {key_of(behav, "DUMMY0033"): bm.Correction("DUMMY003")})
    assert "26 of 38" in run(behav_check.main, *args).output
    folder = "visit one DUMMY015"
    pb.write_dicom_corrections(bids, {folder: "DUMMY015"})
    pb.write_dicom_date_corrections(bids, {folder: bm.dt.date(2002, 6, 24)})
    assert "28 of 38" in run(behav_check.main, *args).output
    assert "25 of 38" in run(behav_check.main, behav, dicom).output  # without them


def test_one_logs_events_are_shown(study: tuple[Path, Path, Path]) -> None:
    bids, behav, dicom = study
    args = (behav, dicom, "--bids", bids, "--log")
    out = run(behav_check.main, *args, key_of(behav, "DUMMY000")).output
    assert "events end at" in out and "block_fixation" in out
    assert "isn't in the BIDS folder yet" in out
    bad = run(behav_check.main, *args, key_of(behav, "DUMMY008"), code=1)
    assert "Can't extract events" in bad.output
    assert "No such log" in run(behav_check.main, *args, "x", code=1).output


def test_the_folders_are_required(study: tuple[Path, Path, Path], tmp_path: Path) -> None:
    for command, n in ((behav_check.main, 1), (bids_check.main, 0), (mriqc.main, 1)):
        assert "Missing argument" in run(command, *[tmp_path] * n, code=2).output


# ---- the BIDS folder ----------------------------------------------------------------------------


def test_bids_check_shows_what_the_crosscheck_shows(tmp_path: Path) -> None:
    bids = tmp_path / "bids"
    for session, volumes in (("1", 10), ("2", 14)):
        write_bold(func(bids, "A", session, "task-Rest_bold.nii.gz"), volumes)
    for n in (1, 2):
        write_bold(func(bids, "A", "2", f"task-Peer_run-{n}_bold.nii.gz"), 10)
    before = contents(bids)
    out = run(bids_check.main, bids, "--files", "--lengths").output
    assert contents(bids) == before
    assert "1 subject(s), 2 session(s)" in out
    assert "keep one: sub-A/ses-2/func/sub-A_ses-2_task-Peer_run-2_bold.nii.gz" in out
    for review in cc.review_bids_folder(bids):
        assert all(issue.text.split(":")[0] in out for issue in review.issues)
    csv = tmp_path / "overview.csv"
    run(bids_check.main, bids, "--csv", csv)
    assert csv.is_file()


# ---- MRIQC --------------------------------------------------------------------------------------


def test_mriqc_runs_and_lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bids, output = tmp_path / "bids", tmp_path / "out"
    make_mriqc_bids(bids)
    ran: list[tuple[str, int, float]] = []

    def run_subject(*args: object, **_kw: object) -> str | None:
        ran.append((str(args[3]), int(str(args[4])), float(str(args[5]))))
        return None if args[3] == "01" else "out of memory"

    monkeypatch.setattr(mq, "docker_status", lambda: (mq.READY, "", Path("docker")))
    monkeypatch.setattr(mriqc, "docker_resources", lambda _docker: (32.0, 8))
    monkeypatch.setattr(mq, "run_subject", run_subject)
    monkeypatch.setattr(mq, "run_logged", lambda *_: 0)
    out = run(mriqc.main, bids, output, code=1).output  # one failed
    assert ran == [("01", 8, 30.0), ("02", 8, 30.0)] and "FAILED: out of memory" in out
    write_iqms(output, "sub-01/ses-1/func/sub-01_ses-1_task-rest_bold.nii.gz", fd_mean=0.2)
    assert am.MODALITY_TEXT["bold"] in run(mriqc.main, bids, output, "--results").output
    unknown = run(mriqc.main, bids, output, "--subject_id", "9", code=1)
    assert "No such subject" in unknown.output


def test_mriqc_says_when_docker_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_mriqc_bids(tmp_path / "bids")
    monkeypatch.setattr(mq, "docker_status", lambda: (mq.DOCKER_MISSING, "no docker here", None))
    out = run(mriqc.main, tmp_path / "bids", tmp_path / "out", code=1).output
    assert "no docker here" in out
