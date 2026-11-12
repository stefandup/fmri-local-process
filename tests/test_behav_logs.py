"""Tests for the Python port of the behavioural regressor Rmds, on synthetic Presentation logs."""

import csv
from pathlib import Path

import pytest
from click.testing import CliRunner

from fmri_local_process.cli import behav_events
from fmri_local_process.processing import behav_logs as bl
from fmri_local_process.processing.behav_compare import compare_with_r

BASE_COLUMNS = [
    "Subject", "Trial", "Event Type", "Code", "Time", "TTime", "Uncertainty", "Duration",
    "Uncertainty", "ReqTime", "ReqDur", "Stim Type", "Pair Index",
]  # fmt: skip


def presentation_log(
    extra_columns: list[str],
    events: list[dict[str, str]],
    responses: list[dict[str, str]] | None = None,
) -> list[str]:
    """Lay lines out where the Rmds expect them: header line 4, data from line 6, the second
    ("Event ...") header 2 lines after the data, its rows 2 lines later, "app" 3 lines after."""
    header = BASE_COLUMNS + extra_columns
    lines = ["Scenario - synthetic", "Logfile written - today", "", "\t".join(header), ""]
    for event in events:
        row = {"Subject": "P001", "Event Type": "Picture", **event}
        lines.append("\t".join(row.get(col, "") for col in header).rstrip("\t"))
    second_header = ["Event Type", "Code", "Type", "Response", "RT"]
    lines += ["", "", "\t".join(second_header), ""]
    for response in responses or []:
        lines.append("\t".join(response.get(col, "") for col in second_header))
    lines += ["", "", "", "Presentation app summary"]
    return lines


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


EMO_COLUMNS = ["block(str)", "block_num(str)", "block_order(str)"]


def emolabeling_rows(complete: bool) -> list[dict[str, str]]:
    rows = [
        {"Event Type": "Pulse", "Time": "100"},
        {"Code": "fix1", "Time": "1000", "block_order(str)": "block_fixation"},
        {"Code": "s1", "Time": "2000", "block(str)": "match", "block_num(str)": "1"},
        {"Code": "s2", "Time": "2500", "block(str)": "match", "block_num(str)": "1"},
        {"Code": "fix2", "Time": "4000", "block_order(str)": "block_fixation"},
        {"Code": "s3", "Time": "5000", "block(str)": "shape", "block_num(str)": "2"},
    ]
    if complete:
        rows += [
            {"Code": "fix3", "Time": "6000", "block_order(str)": "block_fixation"},
            {"Code": "ty", "Time": "7000", "Duration": "3000", "block_order(str)": "thankyou"},
        ]
    else:
        rows.append({"Event Type": "Quit", "Time": "6500"})
    return rows


EMO_RESPONSES = [
    {"Event Type": "Picture", "Code": "s1", "Type": "hit"},
    {"Event Type": "Picture", "Code": "s2", "Type": "incorrect"},
    {"Event Type": "Picture", "Code": "s3", "Type": "miss"},
    {"Event Type": "Picture", "Code": "fix1", "Type": "other"},
]


def test_emolabeling_complete_run() -> None:
    lines = presentation_log(EMO_COLUMNS, emolabeling_rows(complete=True), EMO_RESPONSES)
    table = bl.emolabeling_events(lines)

    assert table.trial_type == [
        "block_fixation", "match", "block_fixation", "shape", "block_fixation", "thankyou",
    ]  # fmt: skip
    # Seconds from the first Pulse (raw time 100, in 0.1 ms units).
    assert table.onset == pytest.approx([0.09, 0.19, 0.39, 0.49, 0.59, 0.69])
    assert table.duration == pytest.approx([0.1, 0.2, 0.1, 0.1, 0.1, 0.3])  # last = thankyou
    assert table.modulator_values == [None, 0.5, None, 0.0, None, None]  # block accuracy


def test_emolabeling_aborted_run_ends_at_quit() -> None:
    lines = presentation_log(EMO_COLUMNS, emolabeling_rows(complete=False), EMO_RESPONSES)
    table = bl.emolabeling_events(lines)

    assert table.trial_type[-1] == "shape"
    assert table.duration[-1] == pytest.approx(0.15)  # Quit (6500) - last onset (5000)


COG_COLUMNS = ["block(str)", "block_num(str)", "repeat_num(str)", "stim(str)"]
COG_ROWS = [
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


def test_cogreappraisal_merges_fixations_and_ratings(tmp_path: Path) -> None:
    table = bl.cogreappraisal_events(presentation_log(COG_COLUMNS, COG_ROWS))

    # The block fixation right after the rating fixation (5000) is folded into it.
    assert table.trial_type == [
        "fixation", "neg-look", "fixation", "neg-decrease", "fixation", "thankyou",
    ]  # fmt: skip
    assert table.onset == pytest.approx([0.09, 0.19, 0.39, 0.59, 0.79, 0.89])
    assert table.duration == pytest.approx([0.1, 0.2, 0.2, 0.2, 0.1, 0.2])
    assert table.modulator_values == [None, "3", None, "4", None, None]

    tsv = tmp_path / "func" / "sub-P001_ses-1_task-Reappraisal_events.tsv"
    bl.write_events_tsv(table, tsv)
    rows = read_tsv(tsv)
    assert list(rows[0]) == ["onset", "duration", "trial_type", "rating"]
    assert rows[0] == {
        "onset": "0.09",
        "duration": "0.1",
        "trial_type": "fixation",
        "rating": "n/a",
    }
    assert rows[1]["rating"] == "3"


def test_without_pulses_onsets_start_at_first_event() -> None:
    table = bl.cogreappraisal_events(presentation_log(COG_COLUMNS, COG_ROWS[1:]))
    assert table.onset[:2] == pytest.approx([0.0, 0.1])


NEWLINE = "\n"
R_CSV_HEADER = "subject,onset,duration,trial_type,rating" + NEWLINE
R_CSV_ROWS = [
    "P001,0,1000,fixation,NA",
    "P001,1000,2000,neg-look,3",
    "P001,3000,2000,fixation,NA",
    "P001,5000,2000,neg-decrease,4",
    "P001,7000,1000,fixation,NA",
    "P001,8000,2000,thankyou,NA",
]


def test_compare_with_r_agrees_after_unit_conversion(tmp_path: Path) -> None:
    table = bl.cogreappraisal_events(presentation_log(COG_COLUMNS, COG_ROWS))
    r_csv = tmp_path / "r.csv"
    r_csv.write_text(R_CSV_HEADER + NEWLINE.join(R_CSV_ROWS) + NEWLINE)
    assert compare_with_r(table, r_csv) == []

    r_csv.write_text(R_CSV_HEADER + NEWLINE.join(R_CSV_ROWS).replace("neg-look,3", "neg-look,5"))
    assert compare_with_r(table, r_csv) == ["row 2 rating: python '3', R '5'"]


def test_behav_events_cli_writes_tsv(tmp_path: Path) -> None:
    log_file = tmp_path / "emolabeling" / "P001-emolabeling.log"
    log_file.parent.mkdir()
    lines = presentation_log(EMO_COLUMNS, emolabeling_rows(complete=True), EMO_RESPONSES)
    log_file.write_text(NEWLINE.join(lines) + NEWLINE)

    result = CliRunner().invoke(behav_events.main, [str(log_file), "-o", str(tmp_path / "out")])

    assert result.exit_code == 0, result.output
    rows = read_tsv(tmp_path / "out" / "sub-P001_task-Matching_events.tsv")
    assert [r["sub_acc"] for r in rows[:2]] == ["n/a", "0.5"]

    no_rmd = CliRunner().invoke(
        behav_events.main, [str(log_file), "--compare"], env={"PHILANI_RMD_DIR": ""}
    )
    assert no_rmd.exit_code == 2 and "--rmd-dir" in no_rmd.output


def test_truncated_log_raises_value_error() -> None:
    with pytest.raises(ValueError):
        bl.cogreappraisal_events(["Scenario", "", "", "Subject\tEvent Type", "", "P001\tPulse"])


def test_find_subject_logs_ignores_other_subjects_and_mac_files(tmp_path: Path) -> None:
    for name in ("P001-emolabeling.log", "._P001-emolabeling.log", "P0011-emolabeling.log"):
        (tmp_path / name).write_text("")
    assert [p.name for p in bl.find_subject_logs(tmp_path, "P001")] == ["P001-emolabeling.log"]
    assert bl.find_subject_logs(tmp_path / "missing", "P001") == []
