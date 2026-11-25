"""Dummy data: the made-up scenarios do what they say, and the dummy twin of a (pretend) real
study matches like it without holding anything of it."""

import random
import re
from collections import Counter
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_behav_match import cog_log, emo_log

from fmri_local_process.app.convert import ConvertJob
from fmri_local_process.cli.generate_sample_data import main
from fmri_local_process.processing import behav_match as bm
from fmri_local_process.processing import dummy_data as dd
from fmri_local_process.processing import philani_bids as pb
from fmri_local_process.processing.behav_logs import canonical_id
from fmri_local_process.processing.dcm2niix import Dcm2niixNotFoundError, find_dcm2niix
from fmri_local_process.processing.dicom_volumes import count_series
from fmri_local_process.processing.dummy_dicom import SeriesShape, write_series
from fmri_local_process.processing.dummy_logs import LogDisguiser, keeps_markers
from fmri_local_process.processing.dummy_names import Disguise, one_character_off
from fmri_local_process.processing.philani_bids import ConversionReport

NAMES = ("Nomvula", "Dlamini", "nomvula@example.org", "Khayelitsha")
REAL_IDS = ("48213", "48214", "77310", "66120", "AB1234")


def matches(behav: Path, dicom: Path) -> list[bm.LogMatch]:
    return bm.match_logs(bm.inspect_logs(behav, {}), pb.scan_sessions(dicom))


def statuses(behav: Path, dicom: Path) -> Counter[str]:
    return Counter(m.status for m in matches(behav, dicom))


# ---- made-up scenarios --------------------------------------------------------------------------


def test_every_scenario_is_matched_as_it_says(tmp_path: Path) -> None:
    out = tmp_path / "dummy"
    report = dd.generate_dummy_dataset(out, 2, with_errors=True, seed=1)
    found = matches(out / "behav", out / "dicom")
    for result in report.participants:
        own = {canonical_id(i) for i in result.log_ids}
        got = sorted(
            m.status
            for m in found
            if m.info.subject in own and m.info.task is not None and m.info.task.task == "Matching"
        )
        assert got == sorted(dd.SCENARIOS[result.scenario].expected), result.scenario
    states = Counter(m.info.state for m in found)
    assert states[bm.ABORTED] == 2 and states[bm.UNREADABLE] == 2
    assert [r.subject_id for r in report.participants][:3] == ["DUMMY000", "DUMMY001", "DUMMY002"]
    log = (out / dd.DUMMY_DATA_LOG_FILENAME).read_text(encoding="utf-8")
    assert all(f"[{name}]" in log for name in dd.SCENARIOS)


def test_only_clean_participants_by_default(tmp_path: Path) -> None:
    report = dd.generate_dummy_dataset(tmp_path / "dummy")
    assert [r.scenario for r in report.participants] == ["clean"] * 5
    assert statuses(tmp_path / "dummy/behav", tmp_path / "dummy/dicom") == {bm.MATCHED: 10}


def test_the_same_seed_gives_the_same_logs(tmp_path: Path) -> None:
    for name in ("a", "b"):
        dd.generate_dummy_dataset(tmp_path / name, 1, True, seed=7)
    logs = [sorted(p.read_text() for p in (tmp_path / n).rglob("*.log")) for n in ("a", "b")]
    assert logs[0] == logs[1]


# ---- a dummy twin of a study --------------------------------------------------------------------


def put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def named(text: str, subject: str) -> str:
    return text.replace("P001", subject)


@pytest.fixture
def study(tmp_path: Path) -> tuple[Path, Path]:
    """A pretend real study with everything that could give a participant away."""
    behav, dicom = tmp_path / "real" / "logs", tmp_path / "real" / "dicom"
    series = ("3_cmrr_tfMRI_labeling_task_AP", "4_cmrr_tfMRI_Cog_reappraisal_task_AP", "2_rest_AP")
    folders = (
        "20240403_0930_48213",
        "20240403_1015_48214",  # another participant, one digit apart
        "20240710_0900_48213",
        "20240501_1341_77310-1",  # twins
        "20240501_1448_77310-2",
        "20240605_0800_AB1234_Dlamini_Nomvula",
        "20240812_1100_66120",
        "Clinical scan Dlamini 28 October",
    )
    for folder in folders:
        for name in series:
            put(dicom / folder / name / "MR.1.2.3.dcm", "not read")
    put(dicom / folders[0] / "Untitled" / series[2] / "MR.1.2.3.dcm", "a copy")
    emo, cog = "emolabeling", "cogreappraisal"

    def log(task: str, raw: str, stamp: str, text: str, where: str = "2024") -> None:
        name = f"{raw}-{task}_scan-{stamp}-QWERT.log"
        put(behav / task / where / name, named(text, raw))

    log(emo, "48213", "20240403-100000", emo_log())
    log(cog, "48213", "20240403-101500", cog_log())
    log(emo, "48214", "20240403-104500", emo_log())
    log(emo, "48213", "20240710-093000", emo_log(), where="Khayelitsha clinic/Nomvula")
    log(emo, "77310-1", "20240501-140000", emo_log())
    log(emo, "77310-2", "20240501-151500", emo_log(complete=False))
    log(emo, "66121", "20240812-113000", emo_log())  # a typo
    log(emo, "AB 1234", "20240605-083000", emo_log())
    log(cog, "66120", "20240101-083000", cog_log())  # another day
    log(emo, "66120", "20240812-113500", emo_log())
    log(emo, "66120", "20240812-114500", emo_log())  # restarted
    notes = "Participant: Nomvula Dlamini\tnomvula@example.org\n"
    put(behav / emo / "99001-emolabeling.log", notes + emo_log())  # notes before the tables
    put(behav / emo / "99002-emolabeling.log", emo_log() + notes)  # and after
    put(behav / emo / "99003-emolabeling.log", emo_log().replace("\t", ",") + "Dlamini,1\n")
    odd = emo_log().replace("s1\t", "Nomvula_happy.bmp\t").replace("extra0(str)", "Dlamini(str)")
    put(behav / emo / "99004-emolabeling.log", odd)
    put(behav / emo / "notes.log", "Dlamini came late\n")
    return behav, dicom


def everything(folder: Path, dicoms: bool = True) -> str:
    """Every name and every log of a folder (its DICOMs' text, too: random digits among it)."""
    parts = []
    for path in folder.rglob("*"):
        parts.append(path.relative_to(folder).as_posix())
        if path.is_file() and (dicoms or path.suffix != ".dcm"):
            parts.append(path.read_bytes().decode("latin-1"))
    return "\n".join(parts)


@pytest.mark.parametrize("seed", range(5))
def test_a_dummy_study_matches_like_the_real_one_and_holds_nothing_of_it(
    study: tuple[Path, Path], tmp_path: Path, seed: int
) -> None:
    behav, dicom = study
    out = tmp_path / "dummy"
    report = dd.generate_dummy_dataset(out, 0, False, behav, dicom, seed=seed)
    assert statuses(out / "behav", out / "dicom") == statuses(behav, dicom)
    assert report.logs == 16 and report.sessions == 8
    assert report.unlike == 0 and report.no_tabs == 2

    text = everything(out)
    for secret in (*NAMES, "QWERT", "happy"):
        assert secret.casefold() not in text.casefold(), secret
    text = everything(out, dicoms=False)
    assert not re.search(r"\bclinic\b|2024(0[1-9]|1[0-2])\d\d|/2024/|\d\d/\d\d/2024", text)
    for real in REAL_IDS:
        assert not re.search(rf"(?<![0-9A-Za-z]){real}(?![0-9A-Za-z])", text), real
    for line in emo_log().splitlines()[5:10]:
        assert line not in text  # no row as it was


def test_the_dummy_keeps_what_tells_participants_apart(
    study: tuple[Path, Path], tmp_path: Path
) -> None:
    behav, dicom = study
    out = tmp_path / "dummy"
    dd.generate_dummy_dataset(out, 0, False, behav, dicom, seed=3)
    raw = [p[2] for d in (out / "dicom").iterdir() if (p := dd.session_name_parts(d.name))]
    people = Counter(canonical_id(r) for r in raw)
    assert sorted(people.values()) == [1, 1, 1, 1, 1, 2]
    twins = sorted(r for r in raw if "-" in r)
    assert [t[-2:] for t in twins] == ["-1", "-2"] and twins[0][:-1] == twins[1][:-1]
    scanned = {r for r in raw if r.isdigit()}
    unknown = {
        m.info.file_subject
        for m in matches(out / "behav", out / "dicom")
        if m.status == bm.UNKNOWN_ID and "_scan-" in m.info.path.name
    }
    assert any(one_character_off(u, s) for u in unknown for s in scanned)  # still a typo


def test_only_the_first_participants(study: tuple[Path, Path], tmp_path: Path) -> None:
    out = tmp_path / "dummy"
    report = dd.generate_dummy_dataset(out, 0, False, *study, n_reference=1, seed=1)
    assert report.sessions == 2 and report.logs == 3  # 48213's two visits


def test_the_real_folders_are_never_written_into(study: tuple[Path, Path], tmp_path: Path) -> None:
    behav, dicom = study
    before = everything(tmp_path / "real")
    for out in (dicom / "dummy", behav, tmp_path / "real"):
        with pytest.raises(dd.DummyDataError, match="real data"):
            dd.generate_dummy_dataset(out, 0, False, behav, dicom)
    put(tmp_path / "used" / "file.txt", "mine")
    with pytest.raises(dd.DummyDataError, match="isn't empty"):
        dd.generate_dummy_dataset(tmp_path / "used", 0, False, behav, dicom)
    assert (tmp_path / "used" / "file.txt").read_text() == "mine"
    dd.generate_dummy_dataset(tmp_path / "fine", 0, False, behav, dicom)
    assert everything(tmp_path / "real") == before


@pytest.mark.parametrize("slip", ["name", "ID in Subject"])
def test_a_leak_deletes_the_dummy_data(
    study: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slip: str
) -> None:
    """The last line of defence, with the disguise broken on purpose."""

    def broken(self: LogDisguiser, text: str) -> str:
        return "Nomvula was here\n" if slip == "name" else "48213\t1\tPicture\n"

    monkeypatch.setattr(LogDisguiser, "text", broken)
    out = tmp_path / "dummy"
    with pytest.raises(dd.LeakError, match="deleted"):
        dd.generate_dummy_dataset(out, 0, False, *study, seed=1)
    assert not out.exists()


def test_dicom_text_tags_are_never_read(
    study: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only whitelisted numbers: a series with a patient's name in its header."""
    import pydicom

    behav, dicom = study
    real = dicom / "20240403_0930_48213" / "2_rest_AP"
    (real / "MR.1.2.3.dcm").unlink()
    write_series(real, SeriesShape(6, planned=9, missing=3, tr_ms=2000), "x", 2, random.Random(1))
    for path in real.iterdir():
        ds = pydicom.dcmread(path)
        ds.PatientName, ds.PatientBirthDate, ds.ProtocolName = "Dlamini^Nomvula", "19900101", "x"
        ds.save_as(path)
    out = tmp_path / "dummy"
    dd.generate_dummy_dataset(out, 0, False, behav, dicom, images=True, seed=2)
    series = [d for d in (out / "dicom").rglob("*_rest_AP") if count_series(d).planned == 9]
    assert len(series) == 1
    counts = count_series(series[0])
    assert (counts.volumes, counts.planned) == (count_series(real).volumes, 9)
    assert counts.missing_images == 3
    ds = pydicom.dcmread(next(series[0].iterdir()))
    assert float(ds.RepetitionTime) == 2000 and "Nomvula" not in everything(out)


def test_images_convert_to_bids(tmp_path: Path) -> None:
    try:
        find_dcm2niix()
    except Dcm2niixNotFoundError:
        pytest.skip("needs dcm2niix")
    raw, bids = tmp_path / "dicom", tmp_path / "bids"
    bids.mkdir()
    rng = random.Random(1)
    folder = raw / "20020304_0900_DUM01"
    write_series(folder / "2_ABCD_T1w_MPR_vNav_RMS", SeriesShape(), "DUM01", 2, rng)
    write_series(folder / "3_cmrr_tfMRI_rest_AP", SeriesShape(12), "DUM01", 3, rng)
    report = ConvertJob(raw, bids, None, False, lambda *_: None).run()
    assert isinstance(report, ConversionReport) and not report.failures
    from fmri_local_process.processing.crosscheck import find_data_files

    found = {d.label: (d.volumes, d.repetition_time) for d in find_data_files(bids)}
    assert found["T1w"][0] == 1 and found["task-Rest_bold"] == (12, pytest.approx(0.832))


# ---- the pieces ---------------------------------------------------------------------------------


def test_ids_are_disguised_consistently() -> None:
    disguise = Disguise(random.Random(1))
    for raw in ("12345", "12345-1", "12345-2", "12346", "ab 99", "X"):
        disguise.learn_id(raw)
    disguise.settle()
    fake = disguise.fake_id("12345")
    assert fake != "12345" and re.fullmatch(r"\d{5}", fake)
    assert disguise.fake_id("12345-1") == f"{fake}-1"
    assert disguise.fake_id("12345T2") == f"{fake}T2"
    assert one_character_off(disguise.fake_id("12346"), fake)
    assert re.fullmatch(r"[a-z]{2} \d{2}", disguise.fake_id("ab 99"))
    assert disguise.fake_id("") == ""


def test_logs_keep_the_lines_the_reader_goes_by() -> None:
    disguise = Disguise(random.Random(1))
    disguise.settle()
    for text in (emo_log(), cog_log(), emo_log(complete=False), "junk\n", "\n\n", ""):
        dummy = LogDisguiser(disguise).text(text)
        assert keeps_markers(text, dummy) and len(dummy.splitlines()) == len(text.splitlines())


def test_the_command_shows_nothing_real(study: tuple[Path, Path], tmp_path: Path) -> None:
    behav, dicom = study
    out = tmp_path / "dummy"
    reference = ["--reference-behav", str(behav), "--reference-dicom", str(dicom)]
    result = CliRunner().invoke(main, [str(out), *reference, "--verbose"])
    assert result.exit_code == 0, result.output
    assert "8 session folder(s), 16 log(s)" in result.output and "DUMMY" not in result.output
    assert not any(n in result.output for n in (*NAMES, *REAL_IDS))
    again = CliRunner().invoke(main, [str(out), *reference])
    assert again.exit_code == 1 and "isn't empty" in again.output
    both = CliRunner().invoke(main, [str(tmp_path / "both"), *reference, "--n-clean", "1"])
    assert "DUMMY000" in both.output and "8 session folder(s)" in both.output


def test_the_command_lists_the_participants(tmp_path: Path) -> None:
    out = tmp_path / "dummy"
    result = CliRunner().invoke(main, [str(out), "--n-clean", "2", "--with-errors", "--seed", "42"])
    assert result.exit_code == 0, result.output
    assert "Generated fMRI dummy data" in result.output
    assert all(name in result.output for name in dd.SCENARIOS)
    assert (out / dd.DUMMY_DATA_LOG_FILENAME).is_file()
    wrong = CliRunner().invoke(main, [str(tmp_path / "x"), "--n-reference", "3"])
    assert wrong.exit_code == 2 and "needs --reference" in wrong.output


def test_generate_and_convert_in_one_call(tmp_path: Path) -> None:
    try:
        find_dcm2niix()
    except Dcm2niixNotFoundError:
        pytest.skip("needs dcm2niix")
    bids = tmp_path / "bids"
    args = [str(tmp_path / "dummy"), "--n-clean", "1", "--seed", "1", "--bids-folder", str(bids)]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert (
        len(list(bids.rglob("*_bold.nii.gz"))) == 3 and len(list(bids.rglob("*_events.tsv"))) == 2
    )
