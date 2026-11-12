"""Behavioural regressors from Presentation .log files -> BIDS events.tsv.

Python port of philani_scripts/behavioural_processing_r_scripts/{emolabeling,cogreappraisal}
_regressors.Rmd (Lior Abramson / Marlie Miles). Deliberate differences from the R:

* One events.tsv per run with the behavioural modulator (sub_acc / rating) as an extra
  column, instead of a "_withrating" + plain pair -- "_events_withrating" isn't a BIDS suffix.
  Columns are onset, duration, trial_type, <modulator>; the redundant `subject` column is dropped.
* Block accuracy loops over blocks by numeric max; R's max() on the text column would stop at
  block 9 if a run had 10+ blocks.
* No "Pulse" rows doesn't empty the table (R's df[-which(...), ] quirk).

* BIDS timing: onset and duration in seconds (Presentation logs 0.1 ms units), with onset
  measured from the first scanner Pulse -- the first volume -- instead of from the first kept
  event. This assumes the first logged pulse is the first volume saved in the BOLD file (no
  discarded dummy volumes). Logs without pulses fall back to the R's first-event zero.
"""

import csv
import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MISSING = "n/a"
TIME_UNITS_PER_SECOND = 10_000  # Presentation logs Time/Duration in 0.1 ms

Row = dict[str, str]


@dataclass
class EventTable:
    modulator: str  # name of the behavioural column, e.g. "rating"
    onset: list[float]
    duration: list[float | None]
    trial_type: list[str]
    modulator_values: list[str | float | None]


def read_log_lines(log_file: Path) -> list[str]:
    return log_file.read_text(encoding="utf-8", errors="replace").splitlines()


def _parse_rows(lines: Sequence[str], header_idx: int, start: int, stop: int) -> list[Row]:
    """Tab-split lines[start:stop] into dicts keyed by lines[header_idx]'s column names.

    Short rows are padded with "" (Presentation omits trailing empty fields). For a repeated
    column name (e.g. "Uncertainty") the first occurrence wins.
    """
    header = lines[header_idx].split("\t")
    rows = []
    for line in lines[start:stop]:
        fields = line.split("\t")
        fields += [""] * (len(header) - len(fields))
        row: Row = {}
        for name, value in zip(header, fields, strict=False):
            row.setdefault(name, value)
        rows.append(row)
    return rows


def _first_section(lines: Sequence[str]) -> tuple[list[Row], int, float | None]:
    """Main event table: header on line 4, data from line 6 to 3 lines before the second
    "Event" header (all 1-based, as in the Rmd).

    Returns (non-pulse rows, 0-based second-header index, time of the first Pulse or None).
    """
    event_lines = [i for i, line in enumerate(lines) if "Event" in line]
    if len(event_lines) < 2:
        raise ValueError("log has no second 'Event' header; not a complete Presentation log")
    second_header = event_lines[1]
    rows = _parse_rows(lines, header_idx=3, start=5, stop=second_header - 2)
    pulses = [_to_float(r["Time"]) for r in rows if r.get("Event Type") == "Pulse"]
    events = [r for r in rows if r.get("Event Type") != "Pulse"]
    return events, second_header, pulses[0] if pulses else None


def _to_float(value: str) -> float:
    return float(value.strip())


def _durations(onsets: list[float]) -> list[float | None]:
    """Each event lasts until the next one starts; the last one is filled in by the caller."""
    return [b - a for a, b in zip(onsets, onsets[1:], strict=False)] + [None]


def _last_duration(trial_types: list[str], onsets: list[float], events: list[Row]) -> float:
    """Normal end: the thank-you screen's logged Duration. Aborted scan: time until Quit."""
    if trial_types[-1] == "thankyou":
        durations = [r["Duration"] for r in events if r["block(str)"] == "thankyou"]
        if not durations:
            raise ValueError("thankyou event has no Duration")
        return _to_float(durations[0])
    quit_times = [r["Time"] for r in events if r["Event Type"] == "Quit"]
    if not quit_times:
        raise ValueError("run ends before thankyou but has no Quit event")
    return _to_float(quit_times[0]) - onsets[-1]


def _block_fixation_rows(events: list[Row]) -> list[int]:
    """Indices of every block fixation and the event right after it (block start), sorted."""
    fixations = [i for i, r in enumerate(events) if r["block(str)"] == "block_fixation"]
    keep = fixations + [i + 1 for i in fixations if i + 1 < len(events)]
    return sorted(keep)


def emolabeling_events(lines: Sequence[str]) -> EventTable:
    """Emotion labeling ("Matching") task: block onsets with block accuracy (sub_acc)."""
    events, second_header, first_pulse = _first_section(lines)
    for r in events:
        if r["block_order(str)"] in ("block_fixation", "thankyou"):
            r["block(str)"] = r["block_order(str)"]

    # Second section (stimulus/response summary) runs to 4 lines above the first "app" line.
    app_line = next((i for i, line in enumerate(lines) if "app" in line), None)
    if app_line is None:
        raise ValueError("log has no 'app' line marking the end of the response section")
    responses = _parse_rows(lines, second_header, start=second_header + 2, stop=app_line - 2)
    responses = [r for r in responses if r.get("Type") in ("hit", "incorrect", "miss")]

    # left_join(df_sub, responses, by = "Code"): one output row per matching response.
    joined: list[tuple[Row, str | None]] = []
    for r in events:
        matches = [resp["Type"] for resp in responses if resp["Code"] == r["Code"]]
        joined += [(r, t) for t in matches] if matches else [(r, None)]
    events_joined = [r for r, _ in joined]

    accuracy: dict[int, float] = {}
    block_nums = [int(r["block_num(str)"]) for r in events_joined if r["block_num(str)"].strip()]
    for block in range(1, max(block_nums, default=0) + 1):
        types = [t for r, t in joined if r["block_num(str)"].strip() == str(block)]
        answered = [t for t in types if t is not None]
        hits = sum(t == "hit" for t in answered)
        accuracy[block] = hits / len(answered) if answered else math.nan

    kept = [events_joined[i] for i in _block_fixation_rows(events_joined)]
    onsets = [_to_float(r["Time"]) for r in kept]
    trial_types = [r["block(str)"] for r in kept]
    sub_acc = [
        accuracy.get(int(r["block_num(str)"])) if r["block_num(str)"].strip() else None
        for r in kept
    ]
    durations = _durations(onsets)
    durations[-1] = _last_duration(trial_types, onsets, events_joined)
    return _bids_table("sub_acc", onsets, durations, trial_types, list(sub_acc), first_pulse)


def cogreappraisal_events(lines: Sequence[str]) -> EventTable:
    """Cognitive reappraisal task: block and fixation onsets with the block's rating."""
    events, _, first_pulse = _first_section(lines)
    for r in events:
        if r["repeat_num(str)"] in ("block_fixation", "thankyou"):
            r["block(str)"] = r["repeat_num(str)"]

    # A Response row belongs to the block/repeat of the event before it.
    for prev, r in zip(events, events[1:], strict=False):
        if r["Event Type"] == "Response":
            r["repeat_num(str)"] = prev["repeat_num(str)"]
            r["block_num(str)"] = prev["block_num(str)"]

    def key(r: Row) -> tuple[str, str, str]:
        return r["Subject"], r["repeat_num(str)"], r["block_num(str)"]

    ratings: dict[tuple[str, str, str], list[str]] = {}
    for r in events:
        if r["Event Type"] == "Response":
            ratings.setdefault(key(r), []).append(r["Code"])

    # merge(..., all = TRUE) then restore the original order: one row per matching rating.
    merged: list[tuple[Row, str | None]] = []
    for r in events:
        merged += [(r, rating) for rating in ratings.get(key(r), [None])]
    merged_rows = [r for r, _ in merged]

    keep = _block_fixation_rows(merged_rows)
    keep += [i for i, r in enumerate(merged_rows) if r["stim(str)"] == "rating_fixation"]
    selected = [merged[i] for i in sorted(keep)]

    onsets: list[float] = []
    trial_types: list[str] = []
    rating_values: list[str | float | None] = []
    for r, rating in selected:
        is_rating_fix = r["stim(str)"] == "rating_fixation"
        onsets.append(_to_float(r["Time"]))
        trial_types.append("rating_fixation" if is_rating_fix else r["block(str)"])
        rating_values.append(None if is_rating_fix else rating)

    # A block fixation straight after a rating fixation is the same fixation period: drop it
    # (the R loop checks rows 2..n-1, so the first and last rows are always kept).
    drop = {
        i
        for i in range(1, len(trial_types) - 1)
        if trial_types[i] == "block_fixation" and trial_types[i - 1] == "rating_fixation"
    }
    onsets = [v for i, v in enumerate(onsets) if i not in drop]
    trial_types = [v for i, v in enumerate(trial_types) if i not in drop]
    rating_values = [v for i, v in enumerate(rating_values) if i not in drop]
    trial_types = [
        "fixation" if t in ("block_fixation", "rating_fixation") else t for t in trial_types
    ]

    durations = _durations(onsets)
    durations[-1] = _last_duration(trial_types, onsets, merged_rows)
    return _bids_table("rating", onsets, durations, trial_types, rating_values, first_pulse)


def _bids_table(
    modulator: str,
    onsets: list[float],
    durations: list[float | None],
    trial_types: list[str],
    modulator_values: list[str | float | None],
    first_pulse: float | None,
) -> EventTable:
    """Raw log times -> seconds from the first scanner pulse (BIDS onset convention)."""
    if not onsets:
        raise ValueError("no block events found in log")
    if first_pulse is None:
        log.warning("No scanner Pulse in log: onsets are relative to the first event instead")
        first_pulse = onsets[0]
    return EventTable(
        modulator,
        [(t - first_pulse) / TIME_UNITS_PER_SECOND for t in onsets],
        [None if d is None else d / TIME_UNITS_PER_SECOND for d in durations],
        trial_types,
        modulator_values,
    )


def _format(value: str | float | None) -> str:
    if isinstance(value, str):
        return value.strip() or MISSING
    if value is None or math.isnan(value):
        return MISSING
    return f"{value:.10g}"


def write_events_tsv(table: EventTable, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(["onset", "duration", "trial_type", table.modulator])
        for row in zip(
            table.onset, table.duration, table.trial_type, table.modulator_values, strict=True
        ):
            writer.writerow([_format(v) for v in row])


@dataclass(frozen=True)
class BehavTask:
    log_subdir: str  # folder under the behavioural log root, e.g. "emolabeling"
    task: str  # BIDS task label the events belong to
    extract: Callable[[Sequence[str]], EventTable]


BEHAV_TASKS = (
    BehavTask("cogreappraisal", "Reappraisal", cogreappraisal_events),
    BehavTask("emolabeling", "Matching", emolabeling_events),
)


def log_subject_id(log_file: Path) -> str:
    """Logs are named <subject id>-<anything>.log."""
    return log_file.name.split("-")[0]


def task_for_log(log_file: Path) -> BehavTask | None:
    """Guess the task from the log's file or folder name (e.g. ".../emolabeling/x.log")."""
    names = (log_file.name.lower(), log_file.parent.name.lower())
    return next((t for t in BEHAV_TASKS if any(t.log_subdir in n for n in names)), None)


def find_subject_logs(log_dir: Path, subject_id: str) -> list[Path]:
    """<subject id>-*.log files in log_dir; macOS "._" resource-fork files are ignored."""
    if not log_dir.is_dir():
        return []
    return sorted(
        p
        for p in log_dir.glob("*.log")
        if not p.name.startswith("._") and log_subject_id(p) == subject_id
    )
