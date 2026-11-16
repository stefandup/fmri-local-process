"""Tests for processing/cpac_docker.py on a synthetic BIDS tree (no Docker needed)."""

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fmri_local_process.processing import cpac_docker
from fmri_local_process.processing.cpac_docker import (
    CPAC_IMAGE,
    RESTART_MARKER,
    STOPPED_MARKER,
    UNKNOWN_EXPECTED_OUTPUTS,
    HangPolicy,
    HangWatch,
    ParallelPlan,
    StartGate,
    bids_subjects,
    bids_tasks,
    bold_sessions,
    build_cpac_command,
    clear_subject_results,
    docker_desktop,
    explain_failure,
    finished_node,
    host_memory_gb,
    is_preprocessed,
    is_subject_done,
    mark_stopped,
    memory_problem,
    missing_outputs,
    parallel_warning,
    preprocess_subject,
    preprocessed_bolds,
    run_cpac,
    subject_console_log,
    subject_tasks,
    suggest_plan,
    was_stopped,
    working_volume,
)


def test_finished_node_parses_nipype_lines() -> None:
    line = '\t [Node] Finished "resample_o", elapsed time 0.871706s.\n'
    assert finished_node(line) == "resample_o"
    assert finished_node('\t [Node] Setting-up "resample_o" in "/tmp/x".\n') is None
    cached = "\t [Job 87] Cached (cpac_sub-1_ses-1.json_space-template_desc-brain_mask_77).\n"
    assert (
        finished_node(cached) == "cpac_sub-1_ses-1.json_space-template_desc-brain_mask_77 (cached)"
    )
    assert finished_node("\t [Job 87] Completed (cpac_sub-1_ses-1.x).\n") is None


def test_run_cpac_streams_lines_and_logs(tmp_path: Path) -> None:
    script = "print('[Node] Finished \"a\"'); print('noise'); print('[Node] Finished \"b\"')"
    console_log = tmp_path / "log" / "out.txt"
    seen: list[str] = []
    returncode = run_cpac([sys.executable, "-c", script], console_log, seen.append)
    assert returncode == 0
    assert [finished_node(line) for line in seen] == ["a", None, "b"]
    assert console_log.read_text(encoding="utf-8").count("Finished") == 2


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


def test_all_tasks_is_done_only_when_every_task_is(tmp_path: Path) -> None:
    """An earlier single-task run must not make 'all tasks' skip the subject."""
    bids = make_bids(tmp_path / "bids")  # P01: task-rest in ses-1 and ses-2
    (bids / "sub-P01/ses-1/func/sub-P01_ses-1_task-Matching_bold.nii.gz").touch()
    assert subject_tasks(bids, "P01") == ["Matching", "rest"]
    out = tmp_path / "out"
    func = out / "output" / "pipeline_cpac_preproc" / "sub-P01" / "ses-1" / "func"
    func.mkdir(parents=True)
    (func / "sub-P01_ses-1_task-Matching_desc-preproc_bold.nii.gz").touch()
    assert is_subject_done(bids, out, "P01", "Matching")
    assert not is_subject_done(bids, out, "P01", None)  # rest not run yet

    for ses in ("1", "2"):
        rest = func.parent.parent / f"ses-{ses}" / "func"
        rest.mkdir(parents=True, exist_ok=True)
        (rest / f"sub-P01_ses-{ses}_task-rest_desc-preproc_bold.nii.gz").touch()
    assert is_subject_done(bids, out, "P01", None)
    assert not is_subject_done(bids, out, "P02", None)  # no BOLD at all

    cmd = build_cpac_command(Path("docker"), bids, out, "P01", CPAC_IMAGE, 1, 3.0, "Matching")
    assert cmd[-2:] == ["--bold_label", "task-Matching"]
    assert "--bold_label" not in build_cpac_command(
        Path("docker"), bids, out, "P01", CPAC_IMAGE, 1, 3.0
    )
    both = ["Matching", "rest"]
    cmd = build_cpac_command(Path("docker"), bids, out, "P01", CPAC_IMAGE, 1, 3.0, both)
    assert cmd[-3:] == ["--bold_label", "task-Matching", "task-rest"]


def test_ticked_tasks_count_only_where_the_subject_has_them(tmp_path: Path) -> None:
    bids = make_bids(tmp_path / "bids")  # P01: task-rest in ses-1 and ses-2
    out = tmp_path / "out"
    for ses in ("1", "2"):
        func = out / "output" / "pipeline_cpac_preproc" / "sub-P01" / f"ses-{ses}" / "func"
        func.mkdir(parents=True)
        (func / f"sub-P01_ses-{ses}_task-rest_desc-preproc_bold.nii.gz").touch()
    # P01 has no Matching run, so ticking Matching as well doesn't make it undone.
    assert is_subject_done(bids, out, "P01", ["rest", "Matching"])
    assert not is_subject_done(bids, out, "P01", ["Matching"])  # nothing to do = not done
    assert bids_subjects(bids, ["Matching", "rest"]) == ["P01"]
    assert bids_subjects(bids, ["Matching"]) == []


def test_is_preprocessed_needs_every_session(tmp_path: Path) -> None:
    out = tmp_path / "out"
    func = out / "output" / "pipeline_cpac_preproc" / "sub-P01" / "ses-1" / "func"
    func.mkdir(parents=True)
    (func / "sub-P01_ses-1_task-rest_space-template_desc-preproc_bold.nii.gz").touch()
    assert is_preprocessed(out, "P01", ["1"])
    assert not is_preprocessed(out, "P01", ["1", "2"])
    assert not is_preprocessed(out, "P0", ["1"])  # no prefix matches
    assert not is_preprocessed(out, "P01", [])


EXPECTED_OUTPUTS = """anat:
- sub-P01*_ses-1*_desc-preproc*_T1w*
func:
- sub-P01*_ses-1*_desc-preproc*_bold*
- sub-P01*_ses-1*_space-MNI152NLin6ASym*_desc-preproc*_bold*
"""


def make_partial_run(out: Path) -> Path:
    """A crashed-while-copying run: the native preproc BOLD written, the MNI one not."""
    log_dir = out / "log" / "pipeline_cpac_preproc" / "sub-P01_ses-1"
    log_dir.mkdir(parents=True)
    (log_dir / "sub-P01_ses-1_expectedOutputs.yml").write_text(EXPECTED_OUTPUTS)
    session = out / "output" / "pipeline_cpac_preproc" / "sub-P01" / "ses-1"
    (session / "anat").mkdir(parents=True)
    (session / "func").mkdir()
    (session / "anat" / "sub-P01_ses-1_desc-preproc_T1w.nii.gz").touch()
    (session / "func" / "sub-P01_ses-1_task-rest_desc-preproc_bold.nii.gz").touch()
    return session


def test_missing_outputs_make_a_run_incomplete(tmp_path: Path) -> None:
    out = tmp_path / "out"
    session = make_partial_run(out)
    assert missing_outputs(out, "P01", ["1"]) == {
        "1": ["func: space-MNI152NLin6ASym_desc-preproc_bold"]
    }
    assert not is_preprocessed(out, "P01", ["1"])

    mni = "sub-P01_ses-1_task-rest_space-MNI152NLin6ASym_desc-preproc_bold.nii.gz"
    (session / "func" / mni).touch()
    assert missing_outputs(out, "P01", ["1"]) == {"1": []}
    assert is_preprocessed(out, "P01", ["1"])
    # Another task's func outputs don't count for this task.
    assert "func: desc-preproc_bold" in missing_outputs(out, "P01", ["1"], "Matching")["1"]
    assert missing_outputs(out, "P01", ["2"]) == {}  # C-PAC never started ses-2

    # A rerun that dies at startup leaves the file empty: unknown, so not complete.
    (out / "log/pipeline_cpac_preproc/sub-P01_ses-1/sub-P01_ses-1_expectedOutputs.yml").write_text(
        ""
    )
    assert missing_outputs(out, "P01", ["1"]) == {"1": [UNKNOWN_EXPECTED_OUTPUTS]}
    assert not is_preprocessed(out, "P01", ["1"])


def test_clear_subject_results_keeps_logs_and_other_subjects(tmp_path: Path) -> None:
    out = tmp_path / "out"
    make_partial_run(out)
    working = out / "working" / "pipeline_cpac_preproc"
    (working / "cpac_sub-P01_ses-1").mkdir(parents=True)
    (working / "cpac_sub-P011_ses-1").mkdir()
    other = out / "output" / "pipeline_cpac_preproc" / "sub-P011"
    other.mkdir()
    removed = clear_subject_results(out, "P01")
    assert removed == [out / "output" / "pipeline_cpac_preproc" / "sub-P01"]
    # Working folders are cleared from Linux (clear_working_volume), never from here.
    assert (working / "cpac_sub-P01_ses-1").exists()
    assert (working / "cpac_sub-P011_ses-1").exists() and other.exists()
    assert (out / "log" / "pipeline_cpac_preproc" / "sub-P01_ses-1").exists()


def test_explain_failure_lost_output_mount(tmp_path: Path) -> None:
    console_log = tmp_path / "log.txt"
    console_log.write_text(
        "OSError: [Errno 5] Input/output error: '/outputs/working/x/result_x.pklz'\n"
    )
    cause = explain_failure(console_log, tmp_path)
    assert cause is not None and cause.startswith("Docker lost access to the output folder")


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
        f"{working_volume(out)}:/outputs/working",
    ]
    assert cmd[cmd.index("--name") + 1] == "fmri_cpac_sub-P01"  # so it can be paused
    tail = cmd[cmd.index(CPAC_IMAGE) :]
    assert tail[:4] == [CPAC_IMAGE, "/bids_dataset", "/outputs", "participant"]
    assert tail[tail.index("--preconfig") + 1] == "preproc"
    assert tail[tail.index("--participant_label") + 1] == "P01"
    assert tail[tail.index("--n_cpus") + 1] == "4"


def test_bids_tasks_and_preprocessed_bolds(tmp_path: Path) -> None:
    bids = make_bids(tmp_path / "bids")
    (bids / "sub-P01/ses-2/func/sub-P01_ses-2_task-Matching_run-2_bold.nii.gz").touch()
    assert bids_tasks(bids) == ["Matching", "rest"]  # crosscheck_pending/ is not a subject

    out = tmp_path / "out"
    func = out / "output" / "pipeline_cpac_preproc" / "sub-P01" / "ses-1" / "func"
    func.mkdir(parents=True)
    wanted = func / "sub-P01_ses-1_task-rest_space-template_desc-preproc_bold.nii.gz"
    wanted.touch()
    (func / "sub-P011_ses-1_task-rest_space-template_desc-preproc_bold.nii.gz").touch()
    assert preprocessed_bolds(out, "P01") == [wanted]


def test_preprocess_subject_reports_a_failed_run(tmp_path: Path) -> None:
    bids = make_bids(tmp_path / "bids")
    out = tmp_path / "out"
    # Python stands in for docker: 'python run --rm ...' exits non-zero straight away.
    failure = preprocess_subject(Path(sys.executable), bids, out, "P01", CPAC_IMAGE, 1, 3.0)
    assert failure is not None and failure.startswith("C-PAC exit")
    assert subject_console_log(out, "P01").is_file()


def test_explain_failure_memory(tmp_path: Path) -> None:
    console_log = tmp_path / "log.txt"
    console_log.write_text(
        "RuntimeError: Insufficient resources available for job:\n"
        "\tanat_reorient_0: 6.175 GB\n\tcheck_transforms: 7 GB\n"
    )
    cause = explain_failure(console_log, tmp_path)
    assert cause is not None and "7.0 GB" in cause and "at least 8 GB" in cause


def test_explain_failure_quotes_the_tool_error(tmp_path: Path) -> None:
    crash = tmp_path / "log" / "crash-x-nuisance_regression.a0.txt"
    crash.parent.mkdir()
    crash.write_text(
        "Stderr:\n\t** ERROR:\x1b[0m total number of fixed regressors (34) is too many for 33 "
        "retained time points!\n\t** FATAL ERROR: Cannot continue\n"
        "FileNotFoundError: No such file or directory 'residuals.nii.gz'\n"
    )
    console_log = tmp_path / "log.txt"
    console_log.write_text(
        "\t Node nuisance_regression.a0 failed to run on host abc.\n"
        f"\t Saving crash info to /outputs/log/{crash.name}\n"
        "\t Node nuisance_regression.a0 failed to run on host abc.\n"
    )
    cause = explain_failure(console_log, tmp_path)
    assert cause is not None
    assert cause.startswith("Step 'nuisance_regression.a0' failed: total number of fixed")
    assert "only 33 usable volumes" in cause
    assert "more failed" not in cause  # the same node twice is one failure


def test_explain_failure_unknown(tmp_path: Path) -> None:
    console_log = tmp_path / "log.txt"
    console_log.write_text("all good\n")
    assert explain_failure(console_log, tmp_path) is None
    assert explain_failure(tmp_path / "missing.txt", tmp_path) is None


def test_docker_desktop_rejects_unknown_actions() -> None:
    with pytest.raises(ValueError):
        docker_desktop("rm -rf")


def test_memory_problem() -> None:
    assert memory_problem(8.0, 15.5, 31.7) is None
    assert memory_problem(8.0, None, None) is None  # unknown: don't block
    too_small_docker = memory_problem(10.0, 7.7, 16.0)
    assert too_small_docker is not None and "only use 7.7 GB" in too_small_docker
    small_host = memory_problem(8.0, 3.8, 4.0)
    assert small_host is not None and "can't run here" in small_host
    assert host_memory_gb() is not None  # readable on the test machine


def test_suggest_plan_splits_docker_limits() -> None:
    # VR-PC-like: 32 cores, 64 GB for Docker -> memory limits it to 6 at once.
    assert suggest_plan(32, 64.0) == ParallelPlan(6, 5, 10.0)
    assert suggest_plan(32, 64.0, n_subjects=2) == ParallelPlan(2, 16, 31.0)
    # Laptop-like: 8 cores, 15.6 GB for Docker -> one subject, all of it.
    assert suggest_plan(8, 15.6) == ParallelPlan(1, 8, 13.0)
    assert suggest_plan(None, None) == ParallelPlan(1, 1, 8.0)  # unknown: the minimum


def test_parallel_warning() -> None:
    assert parallel_warning(ParallelPlan(3, 4, 10.0), 31.0, 12) is None
    too_much = parallel_warning(ParallelPlan(4, 4, 10.0), 31.0, 12)
    assert too_much is not None and "40 GB" in too_much and "16 CPUs" in too_much
    assert parallel_warning(ParallelPlan(4, 4, 10.0), None, None) is None  # unknown


def test_start_gate_spaces_starts() -> None:
    gate = StartGate(0.05)
    started = time.monotonic()
    for _ in range(3):
        gate.wait()
    assert time.monotonic() - started >= 0.1  # 1st at once, then two gaps


def test_explain_failure_container_already_running(tmp_path: Path) -> None:
    console_log = tmp_path / "log.txt"
    console_log.write_text(
        'docker: Error response from daemon: Conflict. The container name "/fmri_cpac_sub-P01" '
        'is already in use by container "abc123".\n'
    )
    cause = explain_failure(console_log, tmp_path)
    assert cause is not None and "already running in Docker" in cause


def test_stopped_marker_ends_the_log(tmp_path: Path) -> None:
    console_log = tmp_path / "log" / "fmri_preprocess_sub-P01.txt"
    assert not was_stopped(console_log)  # no log yet
    console_log.parent.mkdir()
    console_log.write_text("x" * 5000 + "\nKilled\n")
    assert not was_stopped(console_log)
    mark_stopped(console_log)
    assert was_stopped(console_log) and console_log.read_text().endswith(STOPPED_MARKER + "\n")
    cause = explain_failure(console_log, tmp_path)
    assert cause is not None and cause.startswith("Stopped by you")
    console_log.write_text("a new run started\n")  # the next run rewrites the log
    assert not was_stopped(console_log)


# ---- working volume and hang watch --------------------------------------------------------


def test_working_volume_is_per_output_folder(tmp_path: Path) -> None:
    a, b = tmp_path / "out_a", tmp_path / "out_b"
    assert working_volume(a) != working_volume(b)
    assert working_volume(a) == working_volume(Path(str(a).upper()))  # Windows ignores case
    assert working_volume(a).startswith("fmri_cpac_working_")


def test_migrate_working_moves_old_folders_into_the_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    old = out / "working" / "pipeline_cpac_preproc" / "cpac_sub-P01_ses-1"
    old.mkdir(parents=True)
    (old / "result.pklz").write_bytes(b"x")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        shutil.rmtree(old)  # what the script does, from inside the container
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cpac_docker.subprocess, "run", fake_run)
    notes: list[str] = []
    assert cpac_docker.migrate_working(Path("docker"), CPAC_IMAGE, out, "P01", notes.append) is None
    assert not old.exists() and "moving its working folder" in notes[0]
    cmd = calls[0]
    assert 'rm -rf "$dir"' in cmd[-3]  # the old copy is deleted from Linux
    assert cmd[-1] == "P01"  # the subject is an argument, never pasted into the script
    assert f"{working_volume(out)}:/volume" in cmd
    # Nothing left to move: no docker call at all.
    assert cpac_docker.migrate_working(Path("docker"), CPAC_IMAGE, out, "P01", notes.append) is None
    assert len(calls) == 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_hang_watch_needs_quiet_steps_and_idle_cpu() -> None:
    clock = FakeClock()
    cpu = [90.0]
    hangs: list[str] = []
    watch = HangWatch(
        Path("docker"),
        "P01",
        HangPolicy(minutes=30),
        lambda w: hangs.append(w.stuck_on()),
        read_cpu=lambda _docker, _name: cpu[0],
        clock=clock,
    )
    watch.see_line("261113-20:29:14,229 nipype.workflow INFO:\n")
    watch.see_line("                     Currently running:\n")
    watch.see_line("                       * cpac_sub-P01_ses-1.warp_T1mask_74.interp_string\n")
    clock.now = 40 * 60  # 40 quiet minutes, but busy (e.g. ANTs): not hung
    assert not watch.check()
    cpu[0] = 5.0
    clock.now = 60 * 60
    assert not watch.check()  # idle for only 20 min so far
    watch.see_line('\t [Node] Finished "x", elapsed time 1s.\n')  # a step: clock restarts
    clock.now = 85 * 60
    assert not watch.check()
    clock.now = 91 * 60
    assert watch.check() and watch.hung
    assert hangs == ["warp_T1mask_74.interp_string"]
    assert not watch.check()  # fires once per quiet period


def test_unknown_cpu_never_counts_as_idle() -> None:
    clock = FakeClock()
    watch = HangWatch(
        Path("docker"),
        "P01",
        HangPolicy(minutes=1),
        lambda _w: None,
        read_cpu=lambda _docker, _name: None,
        clock=clock,
    )
    clock.now = 600
    assert not watch.check()


class FakeWatch:
    def __init__(self, hung: bool) -> None:
        self.hung = hung

    def stuck_on(self) -> str:
        return "interp_string"


def test_hung_subject_restarts_then_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bids = make_bids(tmp_path / "bids")
    out = tmp_path / "out"
    attempts: list[bool] = []

    def fake_run_watched(*args: object, append: bool = False) -> tuple[int, FakeWatch]:
        attempts.append(append)
        console_log = args[1]
        assert isinstance(console_log, Path)
        console_log.parent.mkdir(parents=True, exist_ok=True)
        with console_log.open("a" if append else "w") as out:
            out.write("Node a failed to run\n")  # stale: must not be the reported cause
        return 137, FakeWatch(hung=True)

    monkeypatch.setattr(cpac_docker, "run_watched", fake_run_watched)
    lines: list[str] = []
    failure = preprocess_subject(
        Path("docker"),
        bids,
        out,
        "P01",
        CPAC_IMAGE,
        1,
        8.0,
        None,
        lines.append,
        hang=HangPolicy(minutes=30, action="restart", max_restarts=2),
        tell=lambda _t: None,
    )
    assert attempts == [False, True, True]  # first run, then 2 restarts appending the log
    assert lines == [RESTART_MARKER, RESTART_MARKER]
    assert failure is not None and failure.startswith("Stopped because C-PAC hung: no progress")
    assert "interp_string, after 2 restart(s)" in failure

    attempts.clear()
    failure = preprocess_subject(
        Path("docker"),
        bids,
        out,
        "P01",
        CPAC_IMAGE,
        1,
        8.0,
        hang=HangPolicy(action="restart"),
        tell=lambda _t: None,
        may_restart=lambda: False,
    )
    assert attempts == [False]  # the user pressed Stop: no restart


def test_explain_failure_reads_only_the_last_attempt(tmp_path: Path) -> None:
    console_log = tmp_path / "log.txt"
    console_log.write_text(
        "Node a failed to run\n"
        f"{RESTART_MARKER}\n"
        "Insufficient resources available for job\n  x: 9.0 GB\n"
    )
    cause = explain_failure(console_log, tmp_path)
    assert cause is not None and cause.startswith("Not enough memory")


def test_folder_size_skips_files_windows_cannot_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.nii.gz").write_bytes(b"x" * 1024)
    (tmp_path / "link.nii.gz").write_bytes(b"")  # stands in for a Linux symlink
    real_lstat = Path.lstat

    def lstat(self: Path) -> object:
        if self.name == "link.nii.gz":
            raise OSError(1920, "The file cannot be accessed by the system")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", lstat)
    assert cpac_docker.folder_size_gb(tmp_path) == 1024 / 1024**3


def test_lost_docker_waits_and_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bids = make_bids(tmp_path / "bids")
    out = tmp_path / "out"
    attempts: list[bool] = []

    def fake_run_cpac(
        cmd: list[str], console_log: Path, on_line: object = None, append: bool = False
    ) -> int:
        attempts.append(append)
        console_log.parent.mkdir(parents=True, exist_ok=True)
        with console_log.open("a" if append else "w") as log_out:
            log_out.write(
                'time="..." level=error msg="error waiting for container: unexpected EOF"\n'
            )
        return 125

    waits: list[str] = []
    monkeypatch.setattr(cpac_docker, "run_cpac", fake_run_cpac)
    monkeypatch.setattr(cpac_docker, "wait_for_docker", lambda *_a, **_k: waits.append("w") or True)
    monkeypatch.setattr(cpac_docker, "stop_containers", lambda *_a, **_k: None)
    lines: list[str] = []
    failure = preprocess_subject(
        Path("docker"),
        bids,
        out,
        "P01",
        CPAC_IMAGE,
        1,
        8.0,
        None,
        lines.append,
        tell=lambda _t: None,
    )
    # First run plus DOCKER_RECOVERIES resumes, each after waiting for Docker.
    assert attempts == [False] + [True] * cpac_docker.DOCKER_RECOVERIES
    assert len(waits) == cpac_docker.DOCKER_RECOVERIES
    assert lines == [RESTART_MARKER] * cpac_docker.DOCKER_RECOVERIES
    assert failure is not None and failure.startswith("Lost contact with Docker")

    attempts.clear()
    preprocess_subject(
        Path("docker"),
        bids,
        out,
        "P01",
        CPAC_IMAGE,
        1,
        8.0,
        tell=lambda _t: None,
        may_restart=lambda: False,
    )
    assert attempts == [False]  # the user pressed Stop: no waiting, no resume


def test_wait_for_docker_gives_up_when_told(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(cpac_docker.subprocess, "run", down)
    checks = iter([True, True, False])
    assert not cpac_docker.wait_for_docker(Path("docker"), lambda: next(checks), poll_s=0)
    up = iter([1, 0])

    def comes_back(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, next(up))

    monkeypatch.setattr(cpac_docker.subprocess, "run", comes_back)
    assert cpac_docker.wait_for_docker(Path("docker"), poll_s=0)
