"""The Qt-free app/ layer: settings, state and the background jobs, with stand-in callbacks and
synthetic data (no Qt, no Docker)."""

import importlib
import os
import pkgutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from matplotlib.figure import Figure
from test_behav_match import emo_log
from test_mriqc import make_bids

from fmri_local_process import app as app_package
from fmri_local_process.app import backup, convert, figures, image_download, preprocess, tabs
from fmri_local_process.app import mriqc as app_mriqc
from fmri_local_process.app.behav import (
    BehavWork,
    load_behav_data,
    parse_date,
    parse_session,
)
from fmri_local_process.app.dicom_volumes import CheckJob
from fmri_local_process.app.events import Event
from fmri_local_process.app.run_progress import BatchProgress
from fmri_local_process.app.settings import JsonSettings, is_on, json_settings, saved_number
from fmri_local_process.app.state import (
    CONVERTING,
    TRIMMING,
    VIEW_ONLY,
    ToolboxState,
    folder_conflict,
    folder_locked,
)
from fmri_local_process.app.trimming import TrimJob
from fmri_local_process.processing import behav_match as bm
from fmri_local_process.processing import mriqc as mq
from fmri_local_process.processing.cpac_docker import RESTART_MARKER, HangPolicy, ParallelPlan
from fmri_local_process.processing.crosscheck import CrosscheckError, DataFile
from fmri_local_process.processing.philani_bids import ConversionReport


def make_state(tmp_path: Path) -> ToolboxState:
    return ToolboxState(lambda app: JsonSettings(tmp_path / "settings" / f"{app}.json"))


# ---- no Qt --------------------------------------------------------------------------------------


def test_app_processing_and_cli_never_load_qt() -> None:
    """In a fresh Python (this one may have Qt loaded already): a headless server has none."""
    code = (
        "import importlib, pkgutil, sys\n"
        "sys.modules['PySide6'] = None  # any import of it fails\n"
        "import fmri_local_process as top\n"
        "for part in ('app', 'processing', 'cli'):\n"
        "    package = importlib.import_module(f'{top.__name__}.{part}')\n"
        "    for m in pkgutil.iter_modules(package.__path__, package.__name__ + '.'):\n"
        "        importlib.import_module(m.name)\n"
        "assert not [m for m in sys.modules if m.startswith('PySide6.')]\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_app_module_has_a_docstring() -> None:
    for module in pkgutil.iter_modules(app_package.__path__, app_package.__name__ + "."):
        assert importlib.import_module(module.name).__doc__, module.name


# ---- events, settings, state --------------------------------------------------------------------


def test_event_calls_every_callback_in_order() -> None:
    seen: list[object] = []
    event = Event()
    event.connect(lambda x: seen.append(("a", x)))
    event.connect(lambda x: seen.append(("b", x)))
    event.emit(1)
    assert seen == [("a", 1), ("b", 1)]


def test_json_settings_survive_a_restart_and_a_damaged_file(tmp_path: Path) -> None:
    path = tmp_path / "config" / "App.json"
    settings = JsonSettings(path)
    settings.setValue("bids", "/data/bids")
    settings.setValue("autosave", True)
    settings.setValue("mriqc_cpus", 4)
    again = JsonSettings(path)
    assert again.value("bids") == "/data/bids" and again.value("gone", 3) == 3
    assert is_on(again.value("autosave")) and saved_number(again, "mriqc_cpus", 1) == 4
    again.remove("bids")
    assert JsonSettings(path).value("bids") is None
    assert [p.name for p in path.parent.iterdir()] == ["App.json"]  # no temporary files left
    path.write_text("{ not json", encoding="utf-8")
    assert JsonSettings(path).value("autosave", "x") == "x"


def test_json_settings_live_in_the_config_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert json_settings("FmriToolbox").path == tmp_path / "fmri_local_process/FmriToolbox.json"


def test_state_remembers_folders_and_announces_changes(tmp_path: Path) -> None:
    bids = tmp_path / "bids"
    bids.mkdir()
    state = make_state(tmp_path)
    changed: list[str] = []
    state.folder_changed.connect(changed.append)
    state.set_folder("bids", bids)
    state.set_folder("bids", bids)  # unchanged: not announced
    assert changed == ["bids"] and state.generation == 1
    assert make_state(tmp_path).folder("bids") == bids
    bids.rmdir()
    assert make_state(tmp_path).folder("bids") is None


def test_state_takes_over_the_old_windows_folders(tmp_path: Path) -> None:
    output = tmp_path / "out"
    output.mkdir()
    JsonSettings(tmp_path / "settings" / "FmriPreprocess.json").setValue("output", str(output))
    assert make_state(tmp_path).folder("output") == output


def test_busy_jobs_lock_the_others(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.set_busy(CONVERTING, True)
    assert state.lock_reason() == "Converting DICOMs to BIDS"
    assert state.lock_reason(CONVERTING) == ""
    assert folder_locked(state, "bids") and not folder_locked(state, "behav")
    state.set_busy(TRIMMING, True)
    assert state.lock_reason(CONVERTING) == "Trimming runs"
    state.set_busy(CONVERTING, False)
    state.set_busy(TRIMMING, False)
    assert state.lock_reason() == ""


def test_folders_may_not_hold_each_other(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.set_folder("bids", tmp_path / "bids")
    assert "its own output" in folder_conflict(state, "dicom", tmp_path / "bids" / "raw")
    assert folder_conflict(state, "dicom", tmp_path / "raw") == ""


def test_header_and_title(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    assert tabs.header_texts(state) == ("No BIDS folder chosen", "(first tab)", "")
    state.set_folder("bids", tmp_path / "study" / "bids")
    state.set_busy(VIEW_ONLY, True)  # the title says so, not the header
    assert tabs.header_texts(state) == ("bids", str(tmp_path / "study"), "")
    assert tabs.title_text(True).endswith("(view only)")
    assert tabs.tab_text(4, "Crosscheck", "6 to check") == "4. Crosscheck (6 to check)"
    assert tabs.tab_names(view_only=False) == list(tabs.TAB_ORDER)


# ---- jobs ---------------------------------------------------------------------------------------


def data_file(path: Path, volumes: int) -> DataFile:
    return DataFile(
        path=path, label="task-Rest_bold", subject="01", session="1", datatype="func",
        pending=False, size=1, volumes=volumes,
    )  # fmt: skip


def test_trim_job_goes_on_after_a_run_that_cannot_be_trimmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fmri_local_process.app import trimming

    def fake_trim(_root: Path, data: DataFile, _volumes: int) -> None:
        if data.path.name == "bad.nii.gz":
            raise CrosscheckError("can't")

    monkeypatch.setattr(trimming, "trim_run", fake_trim)
    lines: list[str] = []
    jobs = [(data_file(tmp_path / name, 20), 10) for name in ("bad.nii.gz", "good.nii.gz")]
    TrimJob(tmp_path, jobs, lines.append).run()
    assert lines == ["NOT TRIMMED  can't", "Trimmed good.nii.gz to 10 volumes (2/2)"]
    assert "good.nii.gz: 20 -> 10" in trimming.confirm_text(jobs)


def test_convert_job_reports_and_explains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    steps: list[tuple[int, int, str]] = []

    def fake_convert(*_args: object, progress: object, **_kw: object) -> ConversionReport:
        assert callable(progress)
        progress(1, 2, "sub-01")
        return ConversionReport()

    monkeypatch.setattr(convert, "find_dcm2niix", lambda: Path("dcm2niix"))
    monkeypatch.setattr(convert, "convert_dicom_root", fake_convert)
    job = convert.ConvertJob(
        tmp_path, tmp_path / "bids", None, False, lambda *step: steps.append(step)
    )
    assert isinstance(job.run(), ConversionReport) and steps == [(1, 2, "sub-01")]

    def missing() -> Path:
        raise convert.Dcm2niixNotFoundError("no dcm2niix")

    monkeypatch.setattr(convert, "find_dcm2niix", missing)
    assert job.run() == "no dcm2niix"


def test_defacer_check_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    def docker_down() -> None:
        raise convert.DockerNeededError("docker is down", installed=True)

    monkeypatch.setattr(convert, "make_deface", docker_down)
    assert convert.check_defacer() == ("docker is down", convert.DOCKER_STOPPED)
    monkeypatch.setattr(convert, "make_deface", lambda: None)
    assert convert.check_defacer() == ("", "")


class Recorder:
    """A listener (PreprocessListener and MriqcListener) that just keeps what it is told."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def on_message(self, *args: object) -> None:
        self.calls.append(("on_message", *args))

    def on_subject_started(self, *args: object) -> None:
        self.calls.append(("on_subject_started", *args))

    def on_step(self, *args: object) -> None:
        self.calls.append(("on_step", *args))

    def on_subject_finished(self, *args: object) -> None:
        self.calls.append(("on_subject_finished", *args))

    def on_subject_done(self, *args: object) -> None:
        self.calls.append(("on_subject_done", *args))

    def on_stage(self, *args: object) -> None:
        self.calls.append(("on_stage", *args))

    def on_batch_done(self, *args: object) -> None:
        self.calls.append(("on_batch_done", *args))

    def on_all_done(self, *args: object) -> None:
        self.calls.append(("on_all_done", *args))

    def named(self, name: str) -> list[tuple]:
        return [call[1:] for call in self.calls if call[0] == name]


def preprocess_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_one: object, **settings: object
) -> tuple[preprocess.PreprocessJob, Recorder]:
    monkeypatch.setattr(preprocess, "find_docker", lambda: Path("docker"))
    monkeypatch.setattr(preprocess, "docker_memory_gb", lambda _docker: 64.0)
    monkeypatch.setattr(preprocess, "host_memory_gb", lambda: 64.0)
    monkeypatch.setattr(preprocess, "image_present", lambda *_: True)
    monkeypatch.setattr(preprocess, "preprocess_subject", run_one)
    monkeypatch.setattr(preprocess, "START_GAP_S", 0.0)
    told = Recorder()
    run = preprocess.RunSettings(
        tmp_path / "bids", tmp_path / "out", ["P01", "P02"], [], 1, 8.0, **settings  # pyright: ignore[reportArgumentType]
    )  # fmt: skip
    return preprocess.PreprocessJob(run, told), told


def test_preprocess_job_counts_steps_and_survives_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run_one(*args: object, **_kw: object) -> str | None:
        subject, on_line = args[3], args[-1]
        assert callable(on_line)
        if subject == "P02":
            raise RuntimeError("boom")
        on_line('[Node] Finished "a".')
        on_line(RESTART_MARKER)  # the resumed run counts its steps again
        on_line('[Node] Finished "a".')
        on_line('[Node] Finished "b".')
        return None

    job, told = preprocess_job(tmp_path, monkeypatch, run_one, mode="resume")
    job.run()
    assert [step for step in told.named("on_step") if step[0] == "P01"] == [
        ("P01", "a", 1),
        ("P01", "restarted after a hang", 0),
        ("P01", "a", 1),
        ("P01", "b", 2),
    ]
    assert sorted(told.named("on_subject_finished")) == [("P01", 2, True), ("P02", 0, False)]
    (summary,) = told.named("on_batch_done")[0]
    assert summary.done == ["P01"] and summary.failed == [("P02", "RuntimeError: boom")]
    assert preprocess.done_lines(summary)[-1] == ("Failed: sub-P02", True)


def test_preprocess_job_stops_the_running_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, killed = threading.Event(), threading.Event()

    def run_one(*_args: object, **_kw: object) -> str | None:
        started.set()
        assert killed.wait(10)
        return "C-PAC exit 137"

    def fake_stop(_docker: Path, _subjects: list[str]) -> None:
        killed.set()

    monkeypatch.setattr(preprocess, "stop_containers", fake_stop)
    job, told = preprocess_job(tmp_path, monkeypatch, run_one, mode="resume")
    thread = threading.Thread(target=job.run)
    thread.start()
    assert started.wait(10)
    assert job.stop_now() == ["P01"]
    thread.join(10)
    (summary,) = told.named("on_batch_done")[0]
    assert summary.stopped and not summary.failed and not summary.done


def test_preprocess_job_says_when_docker_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, told = preprocess_job(tmp_path, monkeypatch, None)

    def down() -> Path:
        raise preprocess.DockerUnavailableError("docker isn't running")

    monkeypatch.setattr(preprocess, "find_docker", down)
    job.run()
    assert told.named("on_batch_done") == [("docker isn't running",)]


def test_run_choices_are_remembered_but_not_a_deleting_mode(tmp_path: Path) -> None:
    settings = JsonSettings(tmp_path / "s.json")
    assert preprocess.read_choices(settings).mode == "skip"
    plan = ParallelPlan(2, 3, 12.0)
    run = preprocess.RunSettings(
        tmp_path, tmp_path, [], [], 3, 12.0, mode="scratch", hang=HangPolicy(45, "stop", 2),
        keep_working=True,
    )  # fmt: skip
    preprocess.save_choices(settings, run, plan)
    preprocess.save_unticked_tasks(settings, {"Rest"})
    again = preprocess.read_choices(settings)
    assert again.plan == plan and again.mode == "skip" and again.keep_working
    assert again.hang == HangPolicy(45, "stop", 2) and again.unticked_tasks == {"Rest"}
    assert preprocess.task_filter(["Rest", "Matching"], {"Rest"}) == ["Matching"]
    assert preprocess.task_filter(["Rest"], set()) == []  # all ticked: every task


def test_mriqc_job_runs_each_subject_then_the_group_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run_subject(*args: object, **_kw: object) -> str | None:
        subject, on_line = args[3], args[6]
        assert callable(on_line)
        on_line('[Node] Finished "mriqc.funcMRIQC.sanitize".')
        return "out of memory" if subject == "02" else None

    monkeypatch.setattr(mq, "run_subject", run_subject)
    monkeypatch.setattr(mq, "run_logged", lambda *_: 0)
    told = Recorder()
    job = app_mriqc.MriqcJob(
        Path("docker"), tmp_path, tmp_path / "out", ["01", "02"], 2, 8.0, mq.EVERYTHING, told
    )
    job.run()
    assert told.named("on_subject_done") == [("01", ""), ("02", "out of memory")]
    assert told.named("on_step")[0] == ("01", 1, "sanitize")
    assert told.named("on_stage") == [("Group step: summary tables and group reports...",)]
    assert told.named("on_all_done") == [("MRIQC finished: 1 done, 1 failed.",)]


def test_mriqc_scans_to_check_follow_the_ticks(tmp_path: Path) -> None:
    bids = tmp_path / "bids"
    make_bids(bids)
    keys = app_mriqc.scan_keys(bids)
    assert keys and all(app_mriqc.scan_text(key) for key in keys)
    tasks = [key for key in keys if key.startswith("task-")]
    everything = app_mriqc.make_selection(keys, set())
    assert everything.tasks is None  # new tasks too
    if tasks:
        assert app_mriqc.make_selection(keys, {tasks[0]}).tasks is not None
    assert app_mriqc.scan_keys(None) == []


def test_dicom_check_can_be_stopped_and_explains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fmri_local_process.processing import dicom_volumes as dv

    stops: list[bool] = []

    def check_runs(_bids: Path, _dicom: Path, _runs: object, progress, stopped) -> list:  # noqa: ANN001
        progress(1, 3)
        stops.append(stopped())
        job.stop()
        stops.append(stopped())
        return []

    monkeypatch.setattr(dv, "check_runs", check_runs)
    seen: list[tuple[int, int]] = []
    job = CheckJob(tmp_path, tmp_path, [], lambda done, total: seen.append((done, total)))
    assert job.run() == ([], "") and seen == [(1, 3)] and stops == [False, True]

    def unreadable(*_args: object) -> list:
        raise OSError("gone")

    monkeypatch.setattr(dv, "check_runs", unreadable)
    assert job.run() == ([], "gone")


def test_batch_progress_texts() -> None:
    batch = BatchProgress()
    batch.start(2, None, "Checking Docker...")
    assert batch.title() == "Checking Docker..."
    batch.subject_started("01")
    batch.step("01", 30, "anat_skullstrip")
    assert batch.title() == "sub-01  -  step 30"
    batch.estimate = 100
    assert "30%" in batch.live_lines("*")[0]
    batch.subject_started("02")
    assert batch.title() == "2 at once: sub-01, sub-02" and batch.mean_steps() == 15
    assert not batch.step("99", 1, "x")  # not running
    batch.subject_finished("01")
    batch.subject_finished("02")
    assert batch.done == 2 and batch.title() == "Next subject..."


# ---- behaviour logs -----------------------------------------------------------------------------


def test_behaviour_corrections_are_matched_then_saved(tmp_path: Path) -> None:
    bids, dicoms, behav = tmp_path / "bids", tmp_path / "dicom", tmp_path / "behav"
    bids.mkdir()
    folder = "20241014_1341_12345-1"
    series = dicoms / folder / "4_labeling_task_AP"
    series.mkdir(parents=True)
    (series / "img.dcm").write_bytes(b"fake")
    log = "emolabeling/12345-2-emolabeling_scan-20241014-151000-BBBBB.log"  # twin 2: a typo
    (behav / log).parent.mkdir(parents=True)
    (behav / log).write_text(emo_log(), encoding="utf-8")

    work = BehavWork(bids, behav, dicoms, load_behav_data(bids, behav, dicoms))
    assert work.matches[0].status == bm.UNKNOWN_ID and work.waiting() == 1
    work.set_verdict(log, bm.REVIEW_OK, "typo in the name")
    work.crosschecked.add(log)
    work.set_corrections({log: ("12345-1", None)})
    work.fixes.checked.add(folder)
    work.match()
    assert work.matches[0].status == bm.MATCHED and work.waiting() == 0
    assert "Matching" in work.folder_cells(0)[10 - 1]  # its Logs cell
    assert not (bids / "code").exists()  # nothing written yet
    work.save()

    again = BehavWork(bids, behav, dicoms, load_behav_data(bids, behav, dicoms))
    assert again.matches[0].status == bm.MATCHED
    assert again.verdicts == {log: (bm.REVIEW_OK, "typo in the name")}
    assert again.crosschecked == {log} and again.fixes.checked == {folder}
    assert "raw DICOMs" in again.export_problem(dicoms / "events")
    assert again.export_problem(tmp_path / "elsewhere") == ""


def test_typed_dates_and_sessions_are_checked() -> None:
    assert parse_date("") is None and str(parse_date("20240403")) == "2024-04-03"
    with pytest.raises(ValueError, match="not a date"):
        parse_date("2024-02-30")
    assert parse_session("") is None and parse_session("02") == "2"
    for wrong in ("0", "x", "-1", "1.5"):
        with pytest.raises(ValueError, match="not a session number"):
            parse_session(wrong)


# ---- downloads, backups, figures ----------------------------------------------------------------


def test_an_unfinished_download_is_remembered(tmp_path: Path) -> None:
    settings = JsonSettings(tmp_path / "s.json")
    info = image_download.ImageState("some/image:1", "the image", "~1 GB", "", settings)
    assert info.download_text() == "Download the image (~1 GB)"
    info.set_partial("42% done")
    again = image_download.ImageState("some/image:1", "the image", "~1 GB", "", settings)
    assert again.download_text() == "Continue download (42% done)"
    again.set_partial("")
    assert settings.value(info.partial_key) is None


def test_both_front_ends_share_the_backup_folder() -> None:
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from fmri_local_process.gui import backup_box

    assert backup.backup_base() == backup_box.backup_base()


def test_figures_are_kept_private_and_named_after_what_they_show() -> None:
    data = figures.png_bytes(Figure(figsize=(1, 1)))
    assert data.startswith(b"\x89PNG")
    path = figures.save_png(data, "sub-01 / run 1: bold")
    assert path.name == "sub-01_run_1_bold.png" and path.read_bytes() == data
    if os.name == "posix":
        assert path.parent.stat().st_mode & 0o077 == 0  # only this user
