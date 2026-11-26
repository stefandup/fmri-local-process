"""The scan cache: unchanged logs and DICOM session folders aren't read again."""

import json
from pathlib import Path

import pytest
from test_behav_match import emo_log, write

from fmri_local_process.app.behav import load_behav_data
from fmri_local_process.processing import behav_match as bm
from fmri_local_process.processing import philani_bids as pb
from fmri_local_process.processing.scan_cache import ScanCache

LOG = "emolabeling/12345-emolabeling_scan-20241014-151000-BBBBB.log"
SHORT = "emolabeling/12345-emolabeling_scan-20241015-151000-CCCCC.log"


def study(tmp_path: Path) -> tuple[Path, Path, Path]:
    bids, raw, behav = tmp_path / "bids", tmp_path / "raw", tmp_path / "behav"
    for folder in ("20241014_1341_12345", "20241015_0900_12345"):
        write(raw / folder / "4_labeling_task_AP" / "img.dcm", "fake")
    write(behav / LOG, emo_log())
    write(behav / SHORT, emo_log(complete=False))
    return bids, raw, behav


def counting(monkeypatch: pytest.MonkeyPatch, module: object, name: str) -> list[object]:
    calls: list[object] = []
    real = getattr(module, name)

    def counted(*args: object, **kwargs: object) -> object:
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def fields(info: bm.LogInfo) -> tuple[object, ...]:
    return (
        info.key, info.task, info.file_subject, info.found_date, info.date_source, info.state,
        info.notes, info.columns, info.pulses, info.trial_types, info.events_end_s,
    )  # fmt: skip


def test_a_second_load_reads_nothing_and_gives_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bids, raw, behav = study(tmp_path)
    first = load_behav_data(bids, behav, raw)
    read = counting(monkeypatch, bm, "inspect_log")
    walked = counting(monkeypatch, pb, "match_series")
    second = load_behav_data(bids, behav, raw)
    assert (read, walked) == ([], [])
    assert [fields(i) for i in second.infos] == [fields(i) for i in first.infos]
    assert second.scanned == first.scanned
    assert any(i.state == bm.ABORTED for i in second.infos)  # notes and state come back too


def test_only_what_changed_is_read_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bids, raw, behav = study(tmp_path)
    load_behav_data(bids, behav, raw)
    write(behav / SHORT, emo_log())  # the log is now complete
    write(raw / "20241015_0900_12345" / "4_labeling_task_AP" / "img2.dcm", "more")
    read = counting(monkeypatch, bm, "inspect_log")
    walked = counting(monkeypatch, pb, "match_series")
    data = load_behav_data(bids, behav, raw)
    assert read == [behav / SHORT]
    assert walked == [raw / "20241015_0900_12345"]
    assert all(i.state == bm.COMPLETE for i in data.infos)


def test_new_scan_rules_or_version_start_afresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bids, raw, behav = study(tmp_path)
    cache = ScanCache.open(behav, raw)
    pb.scan_dicom_root(raw, cache=cache.dicom)
    walked = counting(monkeypatch, pb, "match_series")
    pb.scan_dicom_root(raw, rules=pb.SCAN_RULES[:1], cache=cache.dicom)
    assert len(walked) == 2

    load_behav_data(bids, behav, raw)
    monkeypatch.setattr("fmri_local_process.processing.scan_cache.toolbox_version", lambda: "9.9")
    read = counting(monkeypatch, bm, "inspect_log")
    load_behav_data(bids, behav, raw)
    assert len(read) == 2


def test_a_broken_or_unwritable_cache_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bids, raw, behav = study(tmp_path)
    expected = load_behav_data(bids, behav, raw)
    cache = ScanCache.open(behav, raw)
    assert cache.path is not None and json.loads(cache.path.read_text())["logs"]
    cache.path.write_text("{not json", encoding="utf-8")
    assert load_behav_data(bids, behav, raw).scanned == expected.scanned

    blocked = tmp_path / "a_file"
    blocked.write_text("", encoding="utf-8")
    monkeypatch.setenv("FMRI_CACHE_DIR", str(blocked / "cache"))  # can't make a folder in a file
    assert load_behav_data(bids, behav, raw).scanned == expected.scanned


def test_fresh_reads_everything_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bids, raw, behav = study(tmp_path)
    load_behav_data(bids, behav, raw)
    read = counting(monkeypatch, bm, "inspect_log")
    walked = counting(monkeypatch, pb, "match_series")
    load_behav_data(bids, behav, raw, fresh=True)
    assert (len(read), len(walked)) == (2, 2)
    load_behav_data(bids, behav, raw)  # and the cache is filled again
    assert (len(read), len(walked)) == (2, 2)
