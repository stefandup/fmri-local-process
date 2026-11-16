"""Matching behavioural logs to scan sessions, corrections, and the R parity check.

All logs are synthetic Presentation logs, laid out the way the Rmds read them.
"""

import datetime as dt
import shutil
from pathlib import Path

import pytest

from fmri_local_process.processing import behav_match as bm
from fmri_local_process.processing import philani_bids as pb
from fmri_local_process.processing.behav_compare import compare_with_r, render_r_events
from fmri_local_process.processing.behav_logs import BEHAV_TASKS

TASKS = {t.task: t for t in BEHAV_TASKS}
RMD_DIR = Path(__file__).parents[1] / "behavioural_processing_r_scripts"
BASE_COLUMNS = [
    "Subject", "Trial", "Event Type", "Code", "Time", "TTime", "Uncertainty", "Duration",
    "Uncertainty", "ReqTime", "ReqDur", "Stim Type", "Pair Index",
]  # fmt: skip
EMO_COLUMNS = ["block(str)", "block_num(str)", "block_order(str)"]
COG_COLUMNS = ["block(str)", "block_num(str)", "repeat_num(str)", "stim(str)"]
RESPONSE_COLUMNS = ["Event Type", "Code", "Type", "Response", "RT"]


def padded(columns: list[str], width: int) -> list[str]:
    """Pad a header to the column count the Rmd hard-codes (var_num)."""
    return columns + [f"extra{n}(str)" for n in range(width - len(columns))]


def presentation_log(
    header: list[str],
    events: list[dict[str, str]],
    responses: list[dict[str, str]] | None = None,
    written: str = "Logfile written - 04/03/2024 16:28:22",
    response_width: int = len(RESPONSE_COLUMNS),
) -> str:
    lines = ["Scenario - synthetic", written, "", "\t".join(header), ""]
    for event in events:
        row = {"Subject": "P001", "Event Type": "Picture", **event}
        lines.append("\t".join(row.get(col, "") for col in header).rstrip("\t"))
    second_header = padded(RESPONSE_COLUMNS, response_width)
    lines += ["", "", "\t".join(second_header), ""]
    for response in responses or []:
        lines.append("\t".join(response.get(col, "") for col in second_header).rstrip("\t"))
    lines += ["", "", "", "Presentation app summary"]
    return "\n".join(lines) + "\n"


EMO_EVENTS = [
    {"Event Type": "Pulse", "Time": "100"},
    {"Code": "fix1", "Time": "1000", "block_order(str)": "block_fixation"},
    {"Code": "s1", "Time": "2000", "block(str)": "match", "block_num(str)": "1"},
    {"Code": "s2", "Time": "2500", "block(str)": "match", "block_num(str)": "1"},
    {"Code": "fix2", "Time": "4000", "block_order(str)": "block_fixation"},
    {"Code": "s3", "Time": "5000", "block(str)": "shape", "block_num(str)": "2"},
    {"Code": "fix3", "Time": "6000", "block_order(str)": "block_fixation"},
    {"Code": "ty", "Time": "7000", "Duration": "3000", "block_order(str)": "thankyou"},
]
EMO_RESPONSES = [
    {"Event Type": "Picture", "Code": "s1", "Type": "hit"},
    {"Event Type": "Picture", "Code": "s2", "Type": "incorrect"},
    {"Event Type": "Picture", "Code": "s3", "Type": "hit"},
]
COG_EVENTS = [
    {"Event Type": "Pulse", "Time": "100"},
    {"Time": "1000", "repeat_num(str)": "block_fixation", "stim(str)": "cross"},
    {"Time": "2000", "block(str)": "neg-look", "block_num(str)": "1", "repeat_num(str)": "1"},
    {
        "Time": "4000",
        "block(str)": "neg-look",
        "block_num(str)": "1",
        "repeat_num(str)": "1",
        "stim(str)": "rating_fixation",
    },
    {"Event Type": "Response", "Code": "3", "Time": "4500"},
    {"Time": "5000", "repeat_num(str)": "block_fixation", "stim(str)": "cross"},
    {"Time": "6000", "block(str)": "neg-decrease", "block_num(str)": "2", "repeat_num(str)": "1"},
    {"Event Type": "Response", "Code": "4", "Time": "7000"},
    {"Time": "8000", "repeat_num(str)": "block_fixation", "stim(str)": "cross"},
    {"Time": "9000", "Duration": "2000", "repeat_num(str)": "thankyou"},
]


def emo_log(complete: bool = True, **kwargs: str) -> str:
    events = EMO_EVENTS if complete else [*EMO_EVENTS[:6], {"Event Type": "Quit", "Time": "6500"}]
    header = padded(BASE_COLUMNS + EMO_COLUMNS, bm.R_COLUMN_COUNTS["Matching"])
    return presentation_log(header, events, EMO_RESPONSES, response_width=12, **kwargs)


def cog_log() -> str:
    header = padded(BASE_COLUMNS + COG_COLUMNS, bm.R_COLUMN_COUNTS["Reappraisal"])
    return presentation_log(header, COG_EVENTS)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---- dates, completeness, inventory ---------------------------------------------------------


def test_date_from_file_name_else_log_header() -> None:
    assert bm.filename_date("P001-emo-20240403-162822-ABCDE.log") == dt.date(2024, 4, 3)
    assert bm.filename_date("P001-emo-99999999-20240229.log") == dt.date(2024, 2, 29)
    assert bm.filename_date("12345-emolabeling.log") is None
    assert bm.written_date(["Scenario - x", "Logfile written - 12/31/2024 09:00:00"]) == dt.date(
        2024, 12, 31
    )
    assert bm.written_date(["Scenario - x", "Logfile written - today"]) is None


def test_inspect_flags_aborted_and_unreadable_logs(tmp_path: Path) -> None:
    root = tmp_path / "behav"
    done = bm.inspect_log(write(root / "emolabeling" / "P001-a.log", emo_log()), root)
    stopped = bm.inspect_log(write(root / "emolabeling" / "P002-a.log", emo_log(False)), root)
    broken = bm.inspect_log(write(root / "emolabeling" / "P003-a.log", "junk\n"), root)

    assert (done.state, done.date, done.date_source) == (
        bm.COMPLETE,
        dt.date(2024, 4, 3),
        "log header",
    )
    assert done.pulses == 1 and len(done.columns) == 21 and done.notes == []
    assert done.trial_types == {"block_fixation": 3, "match": 1, "shape": 1, "thankyou": 1}
    assert stopped.state == bm.ABORTED and "before the thank-you" in stopped.notes[-1]
    assert broken.state == bm.UNREADABLE


def test_inventory_counts_layouts_and_trial_types(tmp_path: Path) -> None:
    root = tmp_path / "behav"
    write(root / "emolabeling" / "P001-a.log", emo_log())
    write(root / "emolabeling" / "P002-a.log", emo_log(False))
    write(root / "cogreappraisal" / "P001-a.log", cog_log())

    inv = bm.inventory(bm.inspect_logs(root, {}))

    assert list(inv) == ["Matching", "Reappraisal"]
    assert inv["Matching"].logs == 2
    assert inv["Matching"].states == {bm.COMPLETE: 1, bm.ABORTED: 1}
    assert inv["Matching"].trial_types["thankyou"] == 1  # only the complete run has one
    assert len(inv["Matching"].layouts) == 1


# ---- matching and corrections ---------------------------------------------------------------


def session(subject: str, label: str, day: str, *tasks: str) -> bm.SessionInfo:
    return bm.SessionInfo(subject, label, dt.date.fromisoformat(day), frozenset(tasks))


SESSIONS = [
    session("P001", "1", "2024-04-03", "Matching", "Reappraisal"),
    session("P001", "2", "2024-09-01", "Matching"),
    session("P002", "1", "2024-05-05", "Matching"),
    session("P003", "1", "2024-06-06", "Matching"),
]


def statuses(root: Path, corrections: dict[str, bm.Correction] | None = None) -> dict[str, str]:
    matches = bm.match_logs(bm.inspect_logs(root, corrections or {}), SESSIONS)
    return {
        m.info.key: f"{m.status}|{m.session.name if m.session else ''}|{m.hint}" for m in matches
    }


def test_match_by_date_and_explain_the_rest(tmp_path: Path) -> None:
    root = tmp_path / "behav"
    log = emo_log(written="Logfile written - today")
    write(root / "emolabeling" / "P001-x-20240901-1.log", log)  # matches ses-2
    write(root / "emolabeling" / "P001-x-20240902-1.log", log)  # a day off: date mismatch
    write(root / "emolabeling" / "P009-x-20240606-1.log", log)  # wrong ID? P003 scanned that day
    write(root / "emolabeling" / "P002-x.log", log)  # no date, but only log + only session
    write(root / "cogreappraisal" / "P001-x-20240403-1.log", cog_log())
    write(root / "cogreappraisal" / "P001-x-20240403-2.log", cog_log())  # same session twice
    write(root / "notes" / "P001-x.log", log)

    found = statuses(root)

    assert found == {
        "emolabeling/P001-x-20240901-1.log": "matched|sub-P001_ses-2|",
        "emolabeling/P001-x-20240902-1.log": (
            "date mismatch||scans: ses-1 2024-04-03, ses-2 2024-09-01"
        ),
        "emolabeling/P009-x-20240606-1.log": "unknown ID||same day, no log yet: sub-P003",
        "emolabeling/P002-x.log": "matched|sub-P002_ses-1|",
        "cogreappraisal/P001-x-20240403-1.log": "duplicate|sub-P001_ses-1|same day: pick one",
        "cogreappraisal/P001-x-20240403-2.log": "duplicate|sub-P001_ses-1|same day: pick one",
        "notes/P001-x.log": "no task||no cogreappraisal/emolabeling in path",
    }


def test_corrections_round_trip_and_fix_the_match(tmp_path: Path) -> None:
    root, bids = tmp_path / "behav", tmp_path / "bids"
    write(root / "emolabeling" / "P001-x-20240902-1.log", emo_log())
    write(root / "emolabeling" / "P009-x-20240505-1.log", emo_log())
    corrections = {
        "emolabeling/P001-x-20240902-1.log": bm.Correction(date=dt.date(2024, 9, 1)),
        "emolabeling/P009-x-20240505-1.log": bm.Correction(subject="P002"),
        "emolabeling/unchanged.log": bm.Correction(),  # empty: not written
    }
    bm.write_corrections(bids, corrections)
    loaded = bm.read_corrections(bids)

    assert sorted(loaded) == [
        "emolabeling/P001-x-20240902-1.log",
        "emolabeling/P009-x-20240505-1.log",
    ]
    found = statuses(root, loaded)
    assert found["emolabeling/P001-x-20240902-1.log"] == "matched|sub-P001_ses-2|"
    assert found["emolabeling/P009-x-20240505-1.log"] == "matched|sub-P002_ses-1|"


def test_converter_places_logs_by_date_and_applies_corrections(tmp_path: Path) -> None:
    dicoms, bids, root = tmp_path / "dicom", tmp_path / "bids", tmp_path / "behav"
    bids.mkdir()
    for folder in ("20240403_X_P001", "20240901_X_P001"):
        series = dicoms / folder / "4_labeling_task_AP"
        series.mkdir(parents=True)
        (series / "img.dcm").write_bytes(b"fake")
    write(root / "emolabeling" / "P001-x-20240901-1.log", emo_log())
    write(root / "emolabeling" / "P01-x-20240403-1.log", emo_log())  # typo in the ID
    bm.write_corrections(bids, {"emolabeling/P01-x-20240403-1.log": bm.Correction("P001")})

    def fake_convert(dicom_dir: Path, out_dir: Path, stem: str) -> None:
        (out_dir / f"{stem}.nii.gz").write_bytes(b"n")
        (out_dir / f"{stem}.json").write_text("{}")

    report = pb.convert_dicom_root(dicoms, bids, fake_convert, None, root)

    func = bids / "sub-P001"
    ses1 = func / "ses-1" / "func" / "sub-P001_ses-1_task-Matching_events.tsv"
    ses2 = func / "ses-2" / "func" / "sub-P001_ses-2_task-Matching_events.tsv"
    assert pb.source_log(ses1) == "P01-x-20240403-1.log"
    assert pb.source_log(ses2) == "P001-x-20240901-1.log"
    assert '"SourceLogCorrectedID": "P001"' in ses1.with_suffix(".json").read_text()
    assert report.unplaced == [] and report.to_pick == []


# ---- R parity (temporary, like behav_compare itself) ----------------------------------------


def rscript() -> Path | None:
    found = shutil.which("Rscript")
    if found:
        return Path(found)
    installed = sorted(Path.home().glob("AppData/Local/Programs/R/R-*/bin/Rscript.exe"))
    return installed[-1] if installed else None


@pytest.mark.parametrize("task_label", ["Matching", "Reappraisal"])
def test_python_port_matches_the_r_script(tmp_path: Path, task_label: str) -> None:
    exe = rscript()
    if exe is None or not (RMD_DIR / "emolabeling_regressors.Rmd").is_file():
        pytest.skip("needs Rscript (with tidyverse) and behavioural_processing_r_scripts/")
    task = TASKS[task_label]
    log_file = write(tmp_path / "P001.log", emo_log() if task_label == "Matching" else cog_log())
    table = task.extract(log_file.read_text(encoding="utf-8").splitlines())
    work = tmp_path / "r"
    work.mkdir()

    r_csv = render_r_events(exe, RMD_DIR, task, log_file, "P001", work)

    assert compare_with_r(table, r_csv) == []


def test_behav_check_cli_reports_matches_and_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner
    from rich.console import Console

    from fmri_local_process.cli import behav_check

    dicoms, root = tmp_path / "dicom", tmp_path / "behav"
    series = dicoms / "20240403_X_P001" / "4_labeling_task_AP"
    series.mkdir(parents=True)
    (series / "img.dcm").write_bytes(b"fake")
    write(root / "emolabeling" / "P001-x-20240403-1.log", emo_log())
    write(root / "emolabeling" / "P001-x-20240404-1.log", emo_log(False))

    monkeypatch.setattr(behav_check, "console", Console(width=250))  # no wrapped cells
    result = CliRunner().invoke(behav_check.main, [str(root), str(dicoms), "--problems-only"])

    assert result.exit_code == 0, result.output
    assert "date mismatch" in result.output and "aborted" in result.output
    assert "P001-x-20240403-1.log" not in result.output  # fine: hidden by --problems-only
    assert "1 of 2 log(s) matched" in result.output
    assert "block_fixation" in result.output  # trial-type inventory


# ---- Philani names, verdicts, choosing among same-day logs ----------------------------------


def test_philani_name_gives_date_and_time_even_with_an_all_digit_id(tmp_path: Path) -> None:
    name = "20230101-cogreappraisal_scan-20241014-171511-FBOKV.log"  # ID looks like a date
    assert bm.filename_date(name) == dt.date(2024, 10, 14)
    root = tmp_path / "behav"
    info = bm.inspect_log(write(root / name, cog_log()), root)
    assert (info.file_subject, info.task and info.task.task) == ("20230101", "Reappraisal")
    assert (info.time_text, info.date_source) == ("2024-10-14 17:15:11", "file name")


def test_verdicts_round_trip_and_leave_logs_out(tmp_path: Path) -> None:
    root, bids = tmp_path / "behav", tmp_path / "bids"
    first = "cogreappraisal/P001-cogreappraisal_scan-20240403-101500-AAAAA.log"
    second = "cogreappraisal/P001-cogreappraisal_scan-20240403-103000-BBBBB.log"
    write(root / first, cog_log())
    write(root / second, cog_log())
    assert statuses(root)[first].startswith(bm.DUPLICATE)

    bm.write_corrections(
        bids,
        {
            first: bm.Correction(review=bm.REVIEW_NOT_USED, note="restarted"),
            second: bm.Correction(review=bm.REVIEW_OK),
        },
    )
    found = statuses(root, bm.read_corrections(bids))

    assert found[first] == "excluded||marked 'not used'"
    assert found[second] == "matched|sub-P001_ses-1|"
    assert bm.read_corrections(bids)[first].note == "restarted"


def test_needs_review_until_a_verdict_is_recorded(tmp_path: Path) -> None:
    root = tmp_path / "behav"
    write(root / "emolabeling" / "P001-emolabeling_scan-20240403-101500-AAAAA.log", emo_log(False))
    info = bm.inspect_logs(root, {})[0]
    (match,) = bm.match_logs([info], SESSIONS)
    assert match.status == bm.MATCHED and info.state == bm.ABORTED and bm.needs_review(match)

    info.correction = bm.Correction(review=bm.REVIEW_OK)
    assert not bm.needs_review(bm.match_logs([info], SESSIONS)[0])


def test_converter_uses_the_chosen_log_of_the_day(tmp_path: Path) -> None:
    dicoms, bids, root = tmp_path / "dicom", tmp_path / "bids", tmp_path / "behav"
    bids.mkdir()
    series = dicoms / "20240403_X_P001" / "4_labeling_task_AP"
    series.mkdir(parents=True)
    (series / "img.dcm").write_bytes(b"fake")
    early = "emolabeling/P001-emolabeling_scan-20240403-101500-AAAAA.log"
    late = "emolabeling/P001-emolabeling_scan-20240403-103000-BBBBB.log"
    write(root / early, emo_log(False))
    write(root / late, emo_log())
    bm.write_corrections(bids, {early: bm.Correction(review=bm.REVIEW_ERROR)})

    def fake_convert(dicom_dir: Path, out_dir: Path, stem: str) -> None:
        (out_dir / f"{stem}.nii.gz").write_bytes(b"n")
        (out_dir / f"{stem}.json").write_text("{}")

    report = pb.convert_dicom_root(dicoms, bids, fake_convert, None, root)

    events = bids / "sub-P001" / "ses-1" / "func" / "sub-P001_ses-1_task-Matching_events.tsv"
    assert pb.source_log(events) == Path(late).name
    assert report.unplaced == [] and report.to_pick == []


def test_events_summary_counts_trial_types_and_ordinal_ratings() -> None:
    from fmri_local_process.processing.behav_logs import EventTable

    table = EventTable(
        "rating",
        onset=[0, 10, 20, 30, 40, 50, 60],
        duration=[10, 10, 10, 10, 10, 10, 5],
        trial_type=[
            "fixation", "neg-look", "neg-look", "neg-look", "neg-decrease", "neg-decrease",
            "thankyou",
        ],
        modulator_values=[None, "3", "2", None, "4", "4", None],
    )  # fmt: skip

    lines = bm.events_summary(table)

    assert lines == [
        "7 events, 65.0 s in all",
        "  fixation: 1 event(s), 10.0 s",
        "  neg-look: 3 event(s), 30.0 s",
        "  neg-decrease: 2 event(s), 20.0 s",
        "  thankyou: 1 event(s), 5.0 s",
        "rating (ordinal: counts, median, range):",
        "  neg-look: n=2, median 2.5, range 2-3; [2]x1 [3]x1; 1 without a value",
        "  neg-decrease: n=2, median 4, range 4-4; [4]x2",
        "  all: n=4, median 3.5, range 2-4; [2]x1 [3]x1 [4]x2; 1 without a value",
    ]


def test_events_summary_accuracy_has_no_value_counts() -> None:
    from fmri_local_process.processing.behav_logs import EventTable

    table = EventTable("sub_acc", [0, 1], [1, 1], ["match", "shape"], [0.5, 1.0])
    assert bm.events_summary(table)[-3:] == [
        "  match: n=1, median 0.5, range 0.5-0.5",
        "  shape: n=1, median 1, range 1-1",
        "  all: n=2, median 0.75, range 0.5-1",
    ]


# ---- twins and ID corrections ---------------------------------------------------------------


def test_canonical_ids_for_twins_and_typos() -> None:
    from fmri_local_process.processing.behav_logs import canonical_id, id_problem, raw_log_id

    assert canonical_id("12345-1") == "12345T1"  # twin dash -> T
    assert canonical_id("PILOT-01") == "PILOT01"  # not between digits: just dropped
    assert canonical_id("12345 1") == "123451"
    assert id_problem("12345-1") == "" and "typo" in id_problem("12345 1")
    assert raw_log_id(Path("12345-1-cogreappraisal_scan-20241014-171511-FBOKV.log")) == "12345-1"
    assert raw_log_id(Path("12345-2-emolabeling-old.log")) == "12345-2"
    assert raw_log_id(Path("24-cogreappraisal_scan-20241014-171511-FBOKV.log")) == "24"


TWINS = [  # two twins scanned the same day, an hour apart
    bm.SessionInfo("12345T1", "1", dt.date(2024, 10, 14), frozenset({"Reappraisal"}),
                   "20241014_1341_12345-1", "12345-1", "13:41"),
    bm.SessionInfo("12345T2", "1", dt.date(2024, 10, 14), frozenset({"Reappraisal"}),
                   "20241014_1448_12345-2", "12345-2", "14:48"),
]  # fmt: skip


def test_twin_logs_match_their_own_twin(tmp_path: Path) -> None:
    root = tmp_path / "behav"
    write(root / "12345-1-cogreappraisal_scan-20241014-140000-AAAAA.log", cog_log())
    write(root / "12345-2-cogreappraisal_scan-20241014-151000-BBBBB.log", cog_log())
    write(root / "12345-cogreappraisal_scan-20241014-160000-CCCCC.log", cog_log())  # no twin no.

    matches = {m.info.raw_subject: m for m in bm.match_logs(bm.inspect_logs(root, {}), TWINS)}

    assert matches["12345-1"].session == TWINS[0]
    assert matches["12345-2"].session == TWINS[1]
    assert matches["12345"].status == bm.UNKNOWN_ID
    assert matches["12345"].hint == "no scan session has this ID"  # both twins have a log

    # With twin 2's log missing, the untagged log is offered both same-day twins, with times.
    (root / "12345-2-cogreappraisal_scan-20241014-151000-BBBBB.log").unlink()
    matches = {m.info.raw_subject: m for m in bm.match_logs(bm.inspect_logs(root, {}), TWINS)}
    assert matches["12345"].hint == "same day, no log yet: sub-12345T2 (14:48)"


def test_log_stopped_before_the_first_block_fails_only_that_log(tmp_path: Path) -> None:
    """Regression: an IndexError from such a log used to kill the whole conversion."""
    header = padded(BASE_COLUMNS + EMO_COLUMNS, bm.R_COLUMN_COUNTS["Matching"])
    quit_early = [{"Event Type": "Pulse", "Time": "100"}, {"Event Type": "Quit", "Time": "500"}]
    lines = presentation_log(header, quit_early, response_width=12).splitlines()
    with pytest.raises(ValueError, match="no block events"):
        TASKS["Matching"].extract(lines)
    cog_header = padded(BASE_COLUMNS + COG_COLUMNS, bm.R_COLUMN_COUNTS["Reappraisal"])
    with pytest.raises(ValueError, match="no block events"):
        TASKS["Reappraisal"].extract(presentation_log(cog_header, quit_early).splitlines())

    dicoms, bids, root = tmp_path / "dicom", tmp_path / "bids", tmp_path / "behav"
    bids.mkdir()
    series = dicoms / "20240403_X_P001" / "4_labeling_task_AP"
    series.mkdir(parents=True)
    (series / "img.dcm").write_bytes(b"fake")
    write(
        root / "emolabeling" / "P001-emolabeling_scan-20240403-101500-AAAAA.log", "\n".join(lines)
    )
    write(
        root / "emolabeling" / "P009-emolabeling_scan-20240403-101500-BBBBB.log", "\n".join(lines)
    )

    def fake_convert(dicom_dir: Path, out_dir: Path, stem: str) -> None:
        (out_dir / f"{stem}.nii.gz").write_bytes(b"n")
        (out_dir / f"{stem}.json").write_text("{}")

    report = pb.convert_dicom_root(dicoms, bids, fake_convert, None, root)

    assert report.converted == ["sub-P001_ses-1_task-Matching_bold"]
    assert [why.split(":")[0] for _, why in report.failures] == [
        "P001-emolabeling_scan-20240403-101500-AAAAA.log"
    ]
    assert [key for key, _ in report.unplaced] == [  # no scan session: not even read
        "emolabeling/P009-emolabeling_scan-20240403-101500-BBBBB.log"
    ]
    assert bm.inspect_logs(root, {})[0].state == bm.UNREADABLE
