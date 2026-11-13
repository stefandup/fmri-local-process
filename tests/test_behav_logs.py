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


def write_log(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NEWLINE.join(lines) + NEWLINE)


def test_behav_events_cli_loops_over_folder(tmp_path: Path) -> None:
    logs, out = tmp_path / "logs", tmp_path / "out"
    emo = presentation_log(EMO_COLUMNS, emolabeling_rows(complete=True), EMO_RESPONSES)
    write_log(logs / "emolabeling" / "P001-emolabeling.log", emo)
    write_log(logs / "emolabeling" / "P002-emolabeling.log", emo)
    write_log(logs / "cogreappraisal" / "P001-cogreappraisal.log", ["broken"])
    runner = CliRunner()

    result = runner.invoke(behav_events.main, [str(logs), str(out)])

    assert result.exit_code == 1, result.output  # the broken log fails, the others still run
    assert sorted(p.name for p in out.iterdir()) == [
        "sub-P001_task-Matching_events.tsv",
        "sub-P002_task-Matching_events.tsv",
    ]
    rows = read_tsv(out / "sub-P001_task-Matching_events.tsv")
    assert [r["sub_acc"] for r in rows[:2]] == ["n/a", "0.5"]

    # Re-run with a subject filter: existing output is skipped unless --rerun.
    (out / "sub-P001_task-Matching_events.tsv").write_text("old")
    args = [str(logs / "emolabeling"), str(out), "--subject_id", "P001"]
    assert runner.invoke(behav_events.main, args).exit_code == 0
    assert (out / "sub-P001_task-Matching_events.tsv").read_text() == "old"
    assert runner.invoke(behav_events.main, [*args, "--rerun"]).exit_code == 0
    assert (out / "sub-P001_task-Matching_events.tsv").read_text() != "old"

    no_rmd = runner.invoke(behav_events.main, [*args, "--compare"], env={"PHILANI_RMD_DIR": ""})
    assert no_rmd.exit_code == 2 and "--rmd-dir" in no_rmd.output


def test_plan_log_targets_numbers_repeat_logs_and_flags_unknown_task() -> None:
    logs = [
        Path("emolabeling/P001-emolabeling.log"),
        Path("emolabeling/P001-emolabeling-redo.log"),
        Path("cogreappraisal/P001-x.log"),
        Path("misc/P001-other.log"),
    ]
    targets, unknown = bl.plan_log_targets(logs)

    assert [t.stem for t in targets] == [
        "sub-P001_task-Matching_run-1_events",
        "sub-P001_task-Matching_run-2_events",
        "sub-P001_task-Reappraisal_events",
    ]
    assert unknown == [Path("misc/P001-other.log")]


def test_truncated_log_raises_value_error() -> None:
    with pytest.raises(ValueError):
        bl.cogreappraisal_events(["Scenario", "", "", "Subject\tEvent Type", "", "P001\tPulse"])


def test_find_logs_is_recursive_and_task_comes_from_path_below_root(tmp_path: Path) -> None:
    root = tmp_path / "emolabeling_study"  # an ancestor named like a task must not count
    rel_paths = [
        "cogreappraisal/2024/P001-a.log",
        "._P001-a.log",
        "misc/P002-emolabeling.log",
        "misc/P003-x.log",
    ]
    for rel in rel_paths:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("")

    groups, unknown = bl.group_logs(bl.find_logs(root), root=root)

    assert sorted(groups) == [("P001", "Reappraisal"), ("P002", "Matching")]
    assert unknown == [root / "misc" / "P003-x.log"]


def _flaky_read_text(fail_times: int):
    """A Path.read_text stand-in that raises Errno 22 (no filename, like a mid-read failure)
    for the first `fail_times` calls, then returns two lines."""
    calls = {"n": 0}

    def fake(self: Path, *args: object, **kwargs: object) -> str:
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise OSError(22, "Invalid argument")
        return "a\nb\n"

    return fake


def test_read_log_lines_pins_and_retries_after_failed_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned: list[Path] = []
    monkeypatch.setattr(Path, "read_text", _flaky_read_text(fail_times=1))
    monkeypatch.setattr(bl, "pin_cloud_file", pinned.append)
    log_file = tmp_path / "sub.log"

    assert bl.read_log_lines(log_file) == ["a", "b"]
    assert pinned == [log_file]


def test_read_log_lines_names_the_file_when_retry_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "read_text", _flaky_read_text(fail_times=2))
    monkeypatch.setattr(bl, "pin_cloud_file", lambda path: None)
    log_file = tmp_path / "sub.log"

    with pytest.raises(OSError) as excinfo:
        bl.read_log_lines(log_file)
    assert excinfo.value.errno == 22
    assert excinfo.value.filename == str(log_file)


def test_read_log_lines_does_not_pin_a_readable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned: list[Path] = []
    monkeypatch.setattr(bl, "pin_cloud_file", pinned.append)
    log_file = tmp_path / "sub.log"
    log_file.write_text("x\n", encoding="utf-8")

    assert bl.read_log_lines(log_file) == ["x"]
    assert pinned == []
