"""Offscreen tests of the MRIQC tab and the 'what is missing' banners on the other tabs, on
synthetic folders (no Docker, no real data)."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402 -- needs QT_QPA_PLATFORM set first
from PySide6.QtWidgets import QApplication  # noqa: E402
from test_mriqc import make_bids, write_iqms  # noqa: E402

from fmri_local_process.gui import (  # noqa: E402
    convert_page,
    docker_controls,
    image_download,
    mriqc_page,
    overview_page,
    scan_lengths,
    sequence_page,
    toolbox_state,
)
from fmri_local_process.gui.trimming import Trimmer  # noqa: E402
from fmri_local_process.processing import mriqc as mq  # noqa: E402


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> toolbox_state.ToolboxState:
    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        toolbox_state,
        "make_settings",
        lambda app: QSettings(str(tmp_path / f"{app}.ini"), QSettings.Format.IniFormat),
    )
    monkeypatch.setattr(mriqc_page.MriqcPage, "EMBED_REPORTS", False)
    return toolbox_state.ToolboxState()


def page_with_docker(state: toolbox_state.ToolboxState, docker: str) -> mriqc_page.MriqcPage:
    page = mriqc_page.MriqcPage(state)
    page.probed = True  # no real Docker probe
    page.refresh()
    message = "" if docker == mq.READY else f"Docker says {docker}"
    page.on_probed(docker, message, 8, 16.0)
    return page


def test_banner_says_what_is_missing(state: toolbox_state.ToolboxState, tmp_path: Path) -> None:
    page = page_with_docker(state, mq.DOCKER_STOPPED)
    shown = " | ".join(page.banner.shown)
    assert "No BIDS folder chosen" in shown
    assert "No MRIQC output folder chosen" in shown
    assert "Docker says stopped" in shown
    docker = page.docker_status  # the same line and buttons as on Preprocess
    assert docker.label.text().startswith("⚠ Docker isn't running")
    assert docker.start_button.isEnabled() and docker.restart_button.isEnabled()
    assert not page.run_button.isEnabled()

    empty_bids = tmp_path / "empty_bids"
    empty_bids.mkdir()
    state.set_folder("bids", empty_bids)
    state.set_folder("mriqc", tmp_path / "mriqc")
    page.refresh()
    shown = " | ".join(page.banner.shown)
    assert "No BOLD or T1w scans" in shown and "No MRIQC results" in shown

    page.on_probed(mq.IMAGE_MISSING, "no image", None, None)
    assert page.download.button.isVisibleTo(page)
    assert not page.docker_status.start_button.isEnabled()  # Docker runs: nothing to start
    assert "MRIQC image isn't downloaded" in page.docker_status.label.text()
    assert "background-color: #27ae60" in page.download.button.styleSheet()  # green
    assert not page.download.remove_button.isVisibleTo(page)


def test_results_grouped_by_subject_and_session(
    state: toolbox_state.ToolboxState, tmp_path: Path
) -> None:
    bids, out = tmp_path / "bids", tmp_path / "mriqc"
    make_bids(bids)
    for subject, session, tsnr in (("01", "1", 50), ("01", "2", 52), ("02", "1", 51)):
        name = f"sub-{subject}_ses-{session}_task-rest_bold"
        write_iqms(out, f"sub-{subject}/ses-{session}/func/{name}.nii.gz", tsnr=tsnr)
        (out / f"{name}.html").write_text("<html></html>")
    state.set_folder("bids", bids)
    state.set_folder("mriqc", out)
    page = page_with_docker(state, mq.READY)

    assert page.statuses == {"01": mq.PARTIAL, "02": mq.DONE}  # sub-01's T1w not run
    assert page.to_run() == ["01"]  # done subjects are skipped...
    page.redo_box.setChecked(True)
    assert page.to_run() == ["01", "02"]  # ...unless redone
    assert page.run_button.isEnabled()  # Docker ready, folders chosen, subjects to run
    assert page.download.remove_button.isVisibleTo(page)  # the image is here: can remove
    assert not page.download.button.isVisibleTo(page)

    tree = page.tree
    subjects = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]  # pyright: ignore[reportOptionalMemberAccess]
    assert subjects == ["sub-01", "sub-02"]
    sub_01 = tree.topLevelItem(0)
    assert sub_01 is not None
    sessions = [sub_01.child(i) for i in range(sub_01.childCount())]
    assert [s.text(0) for s in sessions if s is not None] == ["ses-1", "ses-2"]
    ses_1 = sessions[0]
    assert ses_1 is not None
    run = ses_1.child(0)
    assert run is not None and run.text(0) == "task-rest_bold"
    assert page.banner.shown == []  # everything there

    tree.setCurrentItem(run)  # no web engine offscreen: the report is offered in the browser
    assert page.shown_report == out / "sub-01_ses-1_task-rest_bold.html"
    assert page.browser_button.isEnabled()

    page.modality_box.setCurrentIndex(1)  # T1w: none written yet
    assert any("No MRIQC results for T1w" in m for m in page.banner.shown)


def test_download_bar_is_busy_until_docker_reports_layers(
    state: toolbox_state.ToolboxState, tmp_path: Path
) -> None:
    widget = image_download.ImageDownload("some/image:1", "the image", "~1 GB")
    widget.on_progress(-1.0, "Starting the download...")
    assert widget.bar.maximum() == 0  # Qt's moving busy bar
    widget.on_progress(0.5, "1.0 of 2.0 GB downloaded")
    assert widget.bar.maximum() == image_download.PROGRESS_STEPS
    assert widget.bar.value() == image_download.PROGRESS_STEPS // 2
    widget.on_done("TLS handshake timeout. Check the internet connection...")
    assert "Download stopped" in widget.label.text() and widget.button.isEnabled()

    log = tmp_path / "pull.txt"
    log.write_text("latest: Pulling from x\nfailed to copy: TLS handshake timeout\n\n")
    assert image_download.last_line(log) == "failed to copy: TLS handshake timeout"


def test_remove_image_asks_then_offers_download_again(
    state: toolbox_state.ToolboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget = image_download.ImageDownload("some/image:1", "the image", "~1 GB")
    widget.set_present(True)
    widget.set_locked("it is running")
    assert not widget.remove_button.isEnabled()  # never while in use
    widget.set_locked("")
    removed: list[str] = []
    monkeypatch.setattr(image_download, "remove_image", lambda docker, image: removed.append(image))
    monkeypatch.setattr(image_download.shutil, "which", lambda _name: "docker")
    answers = iter([image_download.QMessageBox.StandardButton.No,
                    image_download.QMessageBox.StandardButton.Yes])  # fmt: skip
    monkeypatch.setattr(image_download.QMessageBox, "question", lambda *_: next(answers))
    widget.on_remove()  # No: nothing happens
    assert widget.remover is None and removed == []
    widget.on_remove()
    assert widget.remover is not None
    widget.remover.wait()
    QApplication.processEvents()  # deliver its done signal
    assert removed == ["some/image:1"] and widget.present is False
    assert widget.button.isVisibleTo(widget) and not widget.remove_button.isVisibleTo(widget)


def test_suggest_fills_in_dockers_limits_and_redo_starts_off(
    state: toolbox_state.ToolboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert mq.suggest_resources(12, 31.3) == (12, 29.0)  # 2 GB left for Docker itself
    assert mq.suggest_resources(None, 3.0) == (1, mq.MIN_MEM_GB)
    page = page_with_docker(state, mq.READY)  # Docker: 8 CPUs, 16 GB
    assert (page.cpus.value(), page.memory.value()) == (8, 14.0)  # first time: suggested
    page.cpus.setValue(2)
    assert "Suggested: 8 CPUs, 14 GB" in page.limits_label.text()
    monkeypatch.setattr(page, "check_docker", lambda: None)  # no real Docker
    page.on_suggest()
    assert (page.cpus.value(), page.memory.value()) == (8, 14.0)
    assert page.suggest_pending  # filled in again when Docker answers afresh

    page.redo_box.setChecked(True)
    again = mriqc_page.MriqcPage(state)  # a new session: Redo is never remembered
    assert not again.redo_box.isChecked()


def test_unfinished_download_offers_continue_or_remove(
    state: toolbox_state.ToolboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    def widget() -> image_download.ImageDownload:
        made = image_download.ImageDownload("some/image:1", "the image", "~1 GB",
                                            settings=state.settings)  # fmt: skip
        made.set_present(False)
        return made

    first = widget()
    assert first.button.text() == "Download the image (~1 GB)"
    assert not first.remove_partial_button.isVisibleTo(first)
    monkeypatch.setattr(image_download.PullThread, "start", lambda self: None)  # no docker
    first.start()
    first.on_progress(0.37, "0.4 of 1.0 GB downloaded")
    first.on_done("TLS handshake timeout")  # stopped

    second = widget()  # e.g. after a restart
    assert second.button.text() == "Continue download (37% done)"
    assert second.remove_partial_button.isVisibleTo(second)
    assert "background-color: #e74c3c" in second.remove_partial_button.styleSheet()  # red

    calls: list[str] = []
    monkeypatch.setattr(image_download, "remove_partial", lambda d, image: calls.append(image))
    monkeypatch.setattr(image_download.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(image_download.QMessageBox, "question",
                        lambda *_: image_download.QMessageBox.StandardButton.Yes)  # fmt: skip
    second.on_remove_partial()
    assert second.remover is not None
    second.remover.wait()
    QApplication.processEvents()
    assert calls == ["some/image:1"]
    assert widget().button.text() == "Download the image (~1 GB)"  # forgotten


def test_scan_ticks_choose_subjects_and_are_remembered(
    state: toolbox_state.ToolboxState, tmp_path: Path
) -> None:
    bids, out = tmp_path / "bids", tmp_path / "mriqc"
    make_bids(bids)  # sub-01: T1w + 2 rest runs; sub-02: 1 rest run
    for name in ("sub-01_ses-1_task-rest_bold", "sub-01_ses-2_task-rest_bold"):
        session = name.split("_")[1]
        write_iqms(out, f"sub-01/{session}/func/{name}.nii.gz", tsnr=50)
    state.set_folder("bids", bids)
    state.set_folder("mriqc", out)
    page = page_with_docker(state, mq.READY)
    assert page.scan_keys() == ["T1w", "task-rest"]
    assert page.statuses["01"] == mq.PARTIAL  # its T1w isn't done

    t1w = page.scan_list.item(0)
    assert t1w is not None
    t1w.setCheckState(Qt.CheckState.Unchecked)  # BOLD only
    assert page.selection() == mq.Selection(t1w=False, tasks=None)
    assert page.statuses["01"] == mq.DONE  # every BOLD run done

    again = mriqc_page.MriqcPage(state)
    again.refresh()
    assert again.selection() == mq.Selection(t1w=False, tasks=None)  # remembered
    again.tick_all_scans(False)
    assert any("No scans ticked" in m for m in again.notices())


def test_progress_panel_follows_mriqc_steps(
    state: toolbox_state.ToolboxState, tmp_path: Path
) -> None:
    bids = tmp_path / "bids"
    make_bids(bids)
    state.set_folder("bids", bids)
    page = page_with_docker(state, mq.READY)
    page.progress.start(2, None, "Starting MRIQC...")
    assert page.progress.isVisibleTo(page)
    page.on_subject_started("01")  # no estimate yet: busy bar, steps counted in the title
    assert page.progress.step_bar.maximum() == 0
    for steps in range(1, 31):
        page.on_step("01", steps, "compute_tsnr")
    assert "step 30" in page.progress.title_label.text()
    page.on_subject_done("01", "")
    assert page.saved_number("mriqc_steps_per_scan", 0) == 10.0  # 30 steps, 3 scans

    page.on_subject_started("02")  # 1 scan: ~10 steps expected
    assert page.progress.estimate == 10
    page.on_step("02", 5, "compute_fd")
    assert page.progress.step_bar.value() == 5 and page.progress.step_bar.maximum() == 10
    page.on_subject_done("02", "no IQMs written")  # a failure doesn't change the estimate
    assert page.saved_number("mriqc_steps_per_scan", 0) == 10.0
    page.progress.finish()
    assert not page.progress.isVisibleTo(page)


def test_expand_gives_the_results_the_whole_tab(state: toolbox_state.ToolboxState) -> None:
    page = page_with_docker(state, mq.DOCKER_STOPPED)
    assert page.banner.isVisibleTo(page) and page.expand_button.text() == "Expand ⤢"
    page.expand_button.click()
    assert not page.top_panel.isVisibleTo(page) and not page.subjects_panel.isVisibleTo(page)
    assert page.tree.isVisibleTo(page) and page.report_stack.isVisibleTo(page)
    assert page.expand_button.text() == "Restore ⤡"
    page.expand_button.click()
    assert page.top_panel.isVisibleTo(page) and page.subjects_panel.isVisibleTo(page)
    assert page.banner.isVisibleTo(page)  # its own visibility kept: still has messages


def test_docker_start_and_restart_like_preprocess(
    state: toolbox_state.ToolboxState, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = page_with_docker(state, mq.READY)
    assert page.docker_status.label.text() == "✓ Docker is running."
    actions: list[str] = []
    monkeypatch.setattr(docker_controls, "docker_desktop", lambda action: actions.append(action))
    monkeypatch.setattr(page, "check_docker", lambda forget=True: actions.append("check"))
    page.docker_status.restart_button.click()
    assert page.docker_status.busy and not page.docker_status.restart_button.isEnabled()
    assert "'restart' in progress" in page.docker_status.label.text()
    page.recheck_docker()  # the timer leaves Docker alone while it restarts
    control = page.docker_status.control
    assert control is not None
    control.wait()
    QApplication.processEvents()  # its done signal
    assert actions == ["restart", "check"]  # asked again once it was done
    assert "docker desktop restart" in page.activity.toPlainText()


def test_view_only_hides_running(state: toolbox_state.ToolboxState) -> None:
    state.set_busy(toolbox_state.VIEW_ONLY, True)
    page = mriqc_page.MriqcPage(state)
    page.show_page()
    assert page.probe is None  # no Docker questions in a view-only package
    assert not page.docker_timer.isActive()
    assert not page.run_box.isVisibleTo(page)


def test_other_tabs_say_what_is_missing(state: toolbox_state.ToolboxState, tmp_path: Path) -> None:
    assert any("No DICOM sessions folder" in m for m in convert_page.setup_notices(state))
    assert any("No behavioural logs folder" in m for m in convert_page.setup_notices(state))
    dicom = tmp_path / "dicom"
    dicom.mkdir()
    state.set_folder("dicom", dicom)
    assert any("No session folders in" in m for m in convert_page.setup_notices(state))

    overview = overview_page.OverviewPage(state)
    assert overview.banner.shown == [
        "No BIDS folder chosen: choose it on the Setup & Convert to BIDS tab."
    ]
    bids = tmp_path / "bids"
    bids.mkdir()
    state.set_folder("bids", bids)
    overview.refresh()
    assert "No converted sessions" in overview.banner.shown[0]

    sequence = sequence_page.SequencePage(state)
    assert "No converted sessions" in sequence.banner.shown[0]

    lengths = scan_lengths.ScanLengthsPage(state, Trimmer(state))
    lengths.refresh()
    assert "No BOLD scans" in lengths.banner.shown[0]
