"""Review packages on a synthetic study: only what is chosen goes in, and the records in code/
keep only what is about the shared sessions."""

import json
import os
import stat
from pathlib import Path

import pytest

from fmri_local_process.processing import review_mode
from fmri_local_process.processing import review_package as rp
from fmri_local_process.processing.review_mode import MANIFEST

SCANS = (
    "anat/{p}_T1w.nii.gz",
    "func/{p}_task-Rest_bold.nii.gz",
    "func/{p}_task-Matching_bold.nii.gz",
)


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_study(root: Path) -> rp.Plan:
    """Subjects A and B, sessions 1 and 2 each: BIDS, C-PAC output, logs and code/ records."""
    bids, out, behav = root / "bids", root / "cpac", root / "behav"
    touch(bids / "dataset_description.json", "{}")
    touch(bids / "participants.tsv", "participant_id\tage\nsub-A\t20\nsub-B\t30\n")
    for subject in "AB":
        rows = ["session_id\tacq_time\tsource_folder"]
        for session in "12":
            prefix = f"sub-{subject}_ses-{session}"
            folder = bids / f"sub-{subject}" / f"ses-{session}"
            for scan in SCANS:
                touch(folder / scan.format(p=prefix))
                touch(folder / scan.format(p=prefix).replace(".nii.gz", ".json"), "{}")
            touch(folder / "func" / f"{prefix}_task-Matching_events.tsv", "onset\n1\n")
            rows.append(f"ses-{session}\t2024-0{session}-01\t2024010{session}_X_{subject}")
            func = out / "output" / "pipeline_cpac" / f"sub-{subject}" / f"ses-{session}" / "func"
            touch(func / f"{prefix}_task-Rest_space-MNI_desc-preproc_bold.nii.gz")
            touch(func / f"{prefix}_task-Rest_desc-boldSnrAxial_quality.png")
            touch(func / f"{prefix}_task-Matching_desc-summary_motion.tsv")
            anat = func.parent / "anat"
            touch(anat / f"{prefix}_desc-brain_T1w.nii.gz")
        touch(bids / f"sub-{subject}" / f"sub-{subject}_sessions.tsv", "\n".join(rows) + "\n")
        touch(out / "log" / f"fmri_preprocess_sub-{subject}.txt")
        touch(behav / "emolabeling" / f"{subject}-emolabeling_scan-20240101-100000-1.log")
        touch(behav / "cogreappraisal" / f"{subject}-cogreappraisal_scan-20240101-110000-1.log")
    code = bids / "code"
    touch(code / "expected_volumes.json", json.dumps({"task-Rest_bold": 241}))
    touch(
        code / "session_notes.json",
        json.dumps({"20240101_X_A": "fine", "20240102_X_A": "asleep", "20240101_X_B": "B note"}),
    )
    touch(
        code / "crosscheck_decisions.json",
        json.dumps({"trims": {"20240101_X_A": {"task-Rest_bold": 241}, "20240101_X_B": {}}}),
    )
    touch(
        code / "crosscheck_actions.tsv",
        "time\tuser\taction\ttarget\tdetail\n"
        "t\tu\ttrim\tsub-A/ses-1/func/x.nii.gz\t300 -> 241\n"
        "t\tu\ttrim\tsub-A/ses-2/func/x.nii.gz\t300 -> 241\n"
        "t\tu\tnote\tsub-B_ses-1\thello\n",
    )
    touch(code / "mystery.txt", "who knows")
    return rp.Plan(
        name="review_test",
        bids_root=bids,
        output_root=out,
        behav_root=behav,
        scans=[rp.ScanChoice("A", "1", "T1w"), rp.ScanChoice("A", "1", "task-Rest_bold")],
        outputs=list(rp.OUTPUT_KINDS),
    )


def relative_files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_scan_of_keeps_a_tasks_files_together() -> None:
    assert rp.scan_of("sub-A_ses-1_task-Rest_bold.nii.gz") == "task-Rest_bold"
    assert rp.scan_of("sub-A_ses-1_task-Matching_events.tsv") == "task-Matching_bold"
    assert rp.scan_of("sub-A_ses-1_T1w.json") == "T1w"


def test_only_the_chosen_scans_go_in(tmp_path: Path) -> None:
    plan = make_study(tmp_path)
    assert rp.ScanChoice("B", "2", "task-Matching_bold") in rp.bids_scans(plan.bids_root)
    assert plan.behav_root is not None
    logs = rp.behav_logs(plan.behav_root, plan.bids_root, plan.subjects)
    assert {(b.subject, b.task) for b in logs} == {("A", "Matching"), ("A", "Reappraisal")}
    assert rp.default_behav_logs(logs, plan) == []  # their tasks aren't shared: not ticked
    plan.scans.append(rp.ScanChoice("A", "2", "task-Matching_bold"))
    assert rp.default_behav_logs(logs, plan) == [
        "emolabeling/A-emolabeling_scan-20240101-100000-1.log"
    ]
    plan.scans.pop()
    plan.behav_logs = ["emolabeling/A-emolabeling_scan-20240101-100000-1.log"]

    package = rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)

    bids = relative_files(package / "review_data" / "bids")
    assert {f for f in bids if f.startswith("sub-")} == {
        "sub-A/sub-A_sessions.tsv",
        "sub-A/ses-1/anat/sub-A_ses-1_T1w.nii.gz",
        "sub-A/ses-1/anat/sub-A_ses-1_T1w.json",
        "sub-A/ses-1/func/sub-A_ses-1_task-Rest_bold.nii.gz",
        "sub-A/ses-1/func/sub-A_ses-1_task-Rest_bold.json",
    }
    data = package / "review_data"
    participants = (data / "bids" / "participants.tsv").read_text()
    assert "sub-A" in participants and "sub-B" not in participants
    sessions = (data / "bids" / "sub-A" / "sub-A_sessions.tsv").read_text()
    assert "ses-1" in sessions and "ses-2" not in sessions

    outputs = relative_files(data / "output")
    assert outputs == {
        "output/pipeline_cpac/sub-A/ses-1/func/sub-A_ses-1_task-Rest_space-MNI_desc-preproc_bold.nii.gz",
        "output/pipeline_cpac/sub-A/ses-1/func/sub-A_ses-1_task-Rest_desc-boldSnrAxial_quality.png",
        "output/pipeline_cpac/sub-A/ses-1/anat/sub-A_ses-1_desc-brain_T1w.nii.gz",
        "log/fmri_preprocess_sub-A.txt",
    }  # no Matching motion table (task not shared), nothing of ses-2 or sub-B
    assert relative_files(data / "behav") == {
        "emolabeling/A-emolabeling_scan-20240101-100000-1.log"
    }
    assert not (data / "dicom").exists()  # raw DICOMs only when asked

    manifest = json.loads((package / MANIFEST).read_text())
    assert manifest["subjects"] == ["A"]
    assert manifest["folders"] == {
        "bids": "review_data/bids",
        "output": "review_data/output",
        "behav": "review_data/behav",
    }
    assert (tmp_path / "review" / "review_test.zip").is_file()


def make_mriqc(root: Path) -> Path:
    """MRIQC 24's output for A and B, sessions 1 and 2 (Rest bold and T1w), plus group files."""
    mriqc = root / "mriqc"
    touch(mriqc / "dataset_description.json", "{}")
    touch(
        mriqc / "group_bold.tsv",
        "bids_name\nsub-A_ses-1_task-Rest_bold\nsub-B_ses-1_task-Rest_bold\n",
    )
    touch(mriqc / "group_bold.html")
    touch(mriqc / "logs" / "mriqc-1.log")
    touch(mriqc / "toolbox_logs" / "mriqc_sub-A.txt")
    for subject in "AB":
        for session in "12":
            prefix = f"sub-{subject}_ses-{session}"
            for scan in ("task-Rest_bold", "T1w"):
                touch(mriqc / f"{prefix}_{scan}.html")
                folder = "func" if scan.endswith("bold") else "anat"
                touch(
                    mriqc / f"sub-{subject}" / f"ses-{session}" / folder / f"{prefix}_{scan}.json"
                )
                desc_scan = scan.replace("_bold", "_desc-carpet_bold").replace(
                    "T1w", "desc-zoomed_T1w"
                )
                touch(mriqc / f"sub-{subject}" / "figures" / f"{prefix}_{desc_scan}.svg")
    return mriqc


def test_mriqc_results_of_the_shared_scans_go_in(tmp_path: Path) -> None:
    plan = make_study(tmp_path)
    plan.mriqc_root = make_mriqc(tmp_path)
    plan.scans = [rp.ScanChoice("A", "1", "task-Rest_bold")]  # not its T1w
    plan.make_zip = False

    package = rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)

    assert relative_files(package / "review_data" / "mriqc") == {
        "dataset_description.json",
        "sub-A_ses-1_task-Rest_bold.html",
        "sub-A/ses-1/func/sub-A_ses-1_task-Rest_bold.json",
        "sub-A/figures/sub-A_ses-1_task-Rest_desc-carpet_bold.svg",
    }  # no group reports (every participant), no logs, nothing of ses-2, the T1w or sub-B
    manifest = json.loads((package / MANIFEST).read_text())
    assert manifest["folders"]["mriqc"] == "review_data/mriqc" and manifest["mriqc"]
    assert review_mode.packaged_mriqc(package)

    plan.include_mriqc = False  # left out when unticked
    package = rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)
    assert not (package / "review_data" / "mriqc").exists()
    assert not review_mode.packaged_mriqc(package)

    plan.include_mriqc = True
    plan.scans = [rp.ScanChoice("B", "1", "task-Matching_bold")]  # MRIQC never ran on it
    package = rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)
    assert not (package / "review_data" / "mriqc").exists()  # not just the dataset files


def test_code_records_keep_only_the_shared_sessions(tmp_path: Path) -> None:
    plan = make_study(tmp_path)
    package = rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)
    code = package / "review_data" / "bids" / "code"

    assert json.loads((code / "expected_volumes.json").read_text()) == {"task-Rest_bold": 241}
    assert json.loads((code / "session_notes.json").read_text()) == {"20240101_X_A": "fine"}
    assert json.loads((code / "crosscheck_decisions.json").read_text()) == {
        "trims": {"20240101_X_A": {"task-Rest_bold": 241}}
    }
    actions = (code / "crosscheck_actions.tsv").read_text().splitlines()
    assert actions[0].startswith("time") and len(actions) == 2 and "sub-A/ses-1" in actions[1]
    assert not (code / "mystery.txt").exists()  # unknown records stay home


def test_a_folder_that_is_not_a_package_is_never_replaced(tmp_path: Path) -> None:
    plan = make_study(tmp_path)
    touch(tmp_path / "review" / "review_test" / "precious.txt")
    with pytest.raises(rp.ReviewPackageError, match="isn't a review package"):
        rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)
    plan.make_zip = False
    (tmp_path / "review" / "review_test" / "precious.txt").unlink()
    (tmp_path / "review" / "review_test").rmdir()
    first = rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)
    assert rp.assemble(plan, tmp_path / "review", progress=lambda _text: None) == first


def test_picker_starts_from_the_toolbox_folders_and_the_last_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from fmri_local_process.gui import review_package_dialog as dialog_module
    from fmri_local_process.gui import toolbox_state

    _app = QApplication.instance() or QApplication([])
    settings = tmp_path / "settings"
    monkeypatch.setattr(
        toolbox_state,
        "make_settings",
        lambda app: QSettings(str(settings / f"{app}.ini"), QSettings.Format.IniFormat),
    )
    plan = make_study(tmp_path)
    # What fmri_crosscheck opens on: the older crosscheck window's remembered folders.
    crosscheck = toolbox_state.make_settings("FmriCrosscheck")
    crosscheck.setValue("bids", str(plan.bids_root))
    crosscheck.setValue("behav", str(plan.behav_root))
    crosscheck.sync()
    assert plan.output_root is not None
    toolbox = toolbox_state.make_settings("FmriPreprocess")
    toolbox.setValue("output", str(plan.output_root))
    toolbox.sync()
    fresh = dialog_module.ReviewPackageDialog(tmp_path / "no_plan_yet.json")
    assert fresh.folder("bids") == plan.bids_root
    assert fresh.folder("output") == plan.output_root
    assert fresh.folder("behav") == plan.behav_root
    assert fresh.folder_edits["dicom"].text() == ""  # none remembered: left blank

    plan.behav_logs = ["emolabeling/A-emolabeling_scan-20240101-100000-1.log"]
    plan.outputs = ["preproc_bold", "qc"]
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.to_json())

    dialog = dialog_module.ReviewPackageDialog(plan_file)
    dialog.update_counts()
    again = dialog.current_plan()
    assert (again.name, sorted(again.scans), again.outputs, again.behav_logs) == (
        plan.name,
        sorted(plan.scans),
        plan.outputs,
        plan.behav_logs,
    )
    assert not again.include_dicom
    assert "2 scan(s)" in dialog.summary.text() and dialog.build_button.isEnabled()


def test_preprocess_keeps_its_settings_in_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from fmri_local_process.gui import preprocess_gui

    _app = QApplication.instance() or QApplication([])
    plan = make_study(tmp_path)
    plan.make_zip = False
    package = rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)
    monkeypatch.setenv(review_mode.ENV_VAR, str(package))

    def no_docker() -> Path:
        raise preprocess_gui.DockerUnavailableError("Docker isn't running (test).")

    monkeypatch.setattr(preprocess_gui, "find_docker", no_docker)
    window = preprocess_gui.PreprocessWindow()
    window.memory_probe.wait()
    settings_file = Path(window.settings.fileName())
    assert settings_file.parent == package / "review_data" / "settings"  # not the registry


def test_plan_round_trips_through_json(tmp_path: Path) -> None:
    plan = make_study(tmp_path)
    plan.include_dicom = True
    plan.view_only = False
    plan.mriqc_root = tmp_path / "mriqc"
    plan.include_mriqc = False
    assert rp.Plan.from_json(plan.to_json()) == plan


def test_rebuilding_replaces_a_package_with_read_only_files(tmp_path: Path) -> None:
    plan = make_study(tmp_path)
    plan.make_zip = False
    package = rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)
    for path in package.rglob("*"):  # like C-PAC's logs, copied with their read-only flag
        if path.is_file():
            path.chmod(stat.S_IREAD)
    rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)  # no PermissionError
    assert (package / MANIFEST).is_file()


def test_the_manifest_says_whether_the_package_is_view_only(tmp_path: Path) -> None:
    plan = make_study(tmp_path)
    plan.make_zip = False
    package = rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)
    assert review_mode.view_only(package)  # the default
    plan.view_only = False
    package = rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)
    assert not review_mode.view_only(package)


def test_the_app_opens_on_the_packaged_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from fmri_local_process.gui import toolbox_state

    plan = make_study(tmp_path)
    plan.make_zip = False
    package = rp.assemble(plan, tmp_path / "review", progress=lambda _text: None)
    monkeypatch.setenv(review_mode.ENV_VAR, str(package))

    state = toolbox_state.ToolboxState()
    assert state.folder("bids") == package / "review_data" / "bids"
    assert state.folder("dicom") is None  # not packaged
    other = tmp_path / "elsewhere"
    other.mkdir()
    state.set_folder("bids", other)  # the reviewer's choice is kept in the package
    assert (package / "review_data" / "settings" / "FmriToolbox.ini").is_file()
    assert toolbox_state.ToolboxState().folder("bids") == other
    other.rmdir()  # gone (e.g. another computer): back to the packaged data
    assert toolbox_state.ToolboxState().folder("bids") == package / "review_data" / "bids"
