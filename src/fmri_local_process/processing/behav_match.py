"""Behavioural logs vs scan sessions: which log belongs to which session, and what's wrong.

Lessons from vr_lab_toolbox's crosscheck tools:

* Raw logs are never renamed or edited. A wrong subject ID or date in a log's name is fixed
  by a *correction* recorded in the BIDS folder (code/behav_corrections.json), keyed by the
  log's path below the behavioural root, and applied every time BIDS is built from raw.
* Nothing ambiguous is decided automatically: a log only goes straight into a session when
  its date picks out exactly one of the subject's sessions with that task, and no other log
  claims that session; a single log and a single session with that task belong together even
  when the dates differ (flagged). Everything else is not converted until a human decides.
* After looking at a log's events the human records a verdict: "ok" (issues accepted),
  "error" or "not used" (another log of that day chosen). The last two leave the log out of
  BIDS, so choosing one of several same-day logs lets the chosen one match its session.

A log's date comes from an 8-digit YYYYMMDD in its file name, else from Presentation's
"Logfile written - MM/DD/YYYY ..." header line.
"""

import datetime as dt
import json
import logging
import math
import re
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fmri_local_process.processing.action_log import record_action
from fmri_local_process.processing.behav_logs import (
    BehavTask,
    EventTable,
    canonical_id,
    find_logs,
    id_problem,
    log_subject_id,
    raw_log_id,
    read_log_lines,
    task_for_log,
)
from fmri_local_process.processing.records import write_json_atomic
from fmri_local_process.processing.scan_cache import file_stamp

log = logging.getLogger(__name__)

CORRECTIONS_FILE = Path("code") / "behav_corrections.json"
ISO_DATE = "%Y-%m-%d"
# Philani logs: <ID>-<task>_scan-YYYYMMDD-HHMMSS-<unique id>.log
SCAN_NAME = re.compile(r"_scan-(\d{8})-(\d{6})-")
FILENAME_DATE = re.compile(r"(?<!\d)(\d{8})(?!\d)")  # fallback for other names
WRITTEN_DATE = re.compile(r"Logfile written - (\d{1,2})/(\d{1,2})/(\d{4})")
HEADER_LINES = 3  # the "Logfile written" line is line 2 of a Presentation log
# Column counts hard-coded in the Rmds (var_num): a log with another count breaks the R.
R_COLUMN_COUNTS = {"Matching": 21, "Reappraisal": 18}

# Log states
COMPLETE = "complete"
ABORTED = "aborted"  # ends before the thank-you screen (scan stopped early)
UNREADABLE = "unreadable"

# Match statuses
MATCHED = "matched"
DUPLICATE = "duplicate"  # several logs for one session + task
DATE_MISMATCH = "date mismatch"  # the ID has scans, none on this date
NO_SCAN = "no task scan"  # a session on this date, but without this task's scan
UNKNOWN_ID = "unknown ID"  # no scan session has this subject ID
NO_DATE = "no date"
NO_TASK = "no task"
EXCLUDED = "excluded"  # marked as an error at crosscheck: never converted

# Crosscheck verdicts on a log, after looking at its events
REVIEW_OK = "ok"  # issues seen and accepted
REVIEW_ERROR = "error"  # unusable: left out of BIDS
REVIEW_NOT_USED = "not used"  # fine, but another log of the same day was chosen instead
REVIEWS = (REVIEW_OK, REVIEW_ERROR, REVIEW_NOT_USED)
LEFT_OUT = (REVIEW_ERROR, REVIEW_NOT_USED)


@dataclass(frozen=True)
class Correction:
    """What the human decided about one log: corrected ID/date, a review verdict, and/or
    that they have been through it (crosschecked)."""

    subject: str | None = None
    date: dt.date | None = None
    review: str | None = None  # REVIEW_OK / REVIEW_ERROR
    note: str = ""
    crosschecked: bool = False


def read_corrections(bids_root: Path) -> dict[str, Correction]:
    path = bids_root / CORRECTIONS_FILE
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: Correction(
            value.get("subject") or None,
            dt.datetime.strptime(value["date"], ISO_DATE).date() if value.get("date") else None,
            value.get("review") if value.get("review") in REVIEWS else None,
            value.get("note", ""),
            bool(value.get("crosschecked", False)),
        )
        for key, value in raw.items()
    }


def correction_text(c: Correction | None) -> str:
    """'ID P001, date 2024-04-03, error (note)' for the action log; '-' if nothing set."""
    if c is None:
        return "-"
    parts = [f"ID {c.subject}"] if c.subject else []
    if c.date:
        parts.append(f"date {c.date.strftime(ISO_DATE)}")
    if c.review:
        parts.append(c.review + (f" ({c.note})" if c.note else ""))
    if c.crosschecked:
        parts.append("crosschecked")
    return ", ".join(parts) or "-"


def write_corrections(bids_root: Path, corrections: dict[str, Correction]) -> None:
    path = bids_root / CORRECTIONS_FILE
    old = read_corrections(bids_root) if path.is_file() else {}
    for key in sorted(old.keys() | corrections.keys()):
        before, after = correction_text(old.get(key)), correction_text(corrections.get(key))
        if before != after:
            record_action(bids_root, "correct behaviour log", key, f"{before} -> {after}")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {}
    for key, c in sorted(corrections.items()):
        entry: dict[str, str | bool] = {}
        if c.subject:
            entry["subject"] = c.subject
        if c.date:
            entry["date"] = c.date.strftime(ISO_DATE)
        if c.review:
            entry["review"] = c.review
        if c.note:
            entry["note"] = c.note
        if c.crosschecked:
            entry["crosschecked"] = True
        if entry:
            raw[key] = entry
    write_json_atomic(path, raw)


def filename_time(name: str) -> dt.datetime | None:
    """Date and time from a Philani name (..._scan-YYYYMMDD-HHMMSS-...), if it has one."""
    found = SCAN_NAME.search(name)
    if found is None:
        return None
    try:
        return dt.datetime.strptime("".join(found.groups()), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def filename_date(name: str) -> dt.date | None:
    """The date in a Philani name, else the first 8-digit run that is a real YYYYMMDD date
    (the Philani pattern first, so an all-digit subject ID is never taken for a date)."""
    stamp = filename_time(name)
    if stamp is not None:
        return stamp.date()
    for digits in FILENAME_DATE.findall(name):
        try:
            return dt.datetime.strptime(digits, "%Y%m%d").date()
        except ValueError:
            continue
    return None


def written_date(lines: list[str]) -> dt.date | None:
    """Date on Presentation's "Logfile written - MM/DD/YYYY hh:mm:ss" header line."""
    for line in lines[:HEADER_LINES]:
        found = WRITTEN_DATE.search(line)
        if found:
            month, day, year = (int(g) for g in found.groups())
            try:
                return dt.date(year, month, day)
            except ValueError:
                return None
    return None


@dataclass
class LogInfo:
    path: Path
    key: str  # path below the behavioural root (posix): the corrections key
    task: BehavTask | None
    file_subject: str  # ID from the file name, canonical (twin 12345-1 -> 12345T1)
    found_date: dt.date | None  # date as found in the file name / log header
    date_source: str  # "file name", "log header" or ""
    correction: Correction = field(default_factory=Correction)
    state: str = COMPLETE
    notes: list[str] = field(default_factory=list)
    columns: tuple[str, ...] = ()
    pulses: int = 0
    trial_types: Counter[str] = field(default_factory=Counter)
    events_end_s: float | None = None  # when the last event ends, from the first pulse

    @property
    def raw_subject(self) -> str:
        """The ID exactly as written in the file name."""
        return raw_log_id(self.path)

    @property
    def subject(self) -> str:
        return (
            canonical_id(self.correction.subject) if self.correction.subject else self.file_subject
        )

    @property
    def date(self) -> dt.date | None:
        return self.correction.date or self.found_date

    @property
    def corrected(self) -> bool:
        """ID or date overridden at crosscheck (a review verdict alone doesn't count)."""
        return bool(self.correction.subject or self.correction.date)

    @property
    def excluded(self) -> bool:
        """Marked as an error, or another log was chosen: left out of BIDS."""
        return self.correction.review in LEFT_OUT

    @property
    def time_text(self) -> str:
        """'2024-10-14 17:15:11' when the name has a time, else just the date, or '-'."""
        stamp = filename_time(self.path.name)
        if stamp is not None and not self.correction.date:
            return f"{stamp:%Y-%m-%d %H:%M:%S}"
        return f"{self.date:%Y-%m-%d}" if self.date else "-"


def inspect_log(log_file: Path, root: Path, correction: Correction | None = None) -> LogInfo:
    """Read one log: date, completeness, columns, and the trial types it would produce."""
    info = LogInfo(
        path=log_file,
        key=log_file.relative_to(root).as_posix() if log_file.is_relative_to(root) else "",
        task=task_for_log(log_file, root),
        file_subject=log_subject_id(log_file),
        found_date=filename_date(log_file.name),
        date_source="file name",
        correction=correction or Correction(),
    )
    if problem := id_problem(raw_log_id(log_file)):
        info.notes.append(f"ID {problem}")
    try:
        lines = read_log_lines(log_file)
    except OSError as e:
        info.state = UNREADABLE
        info.notes.append(f"can't read: {e}")
        if info.found_date is None:
            info.date_source = ""
        return info

    if info.found_date is None:
        info.found_date = written_date(lines)
        info.date_source = "log header" if info.found_date else ""
    if len(lines) > HEADER_LINES:
        info.columns = tuple(lines[HEADER_LINES].split("\t"))
    info.pulses = sum("\tPulse\t" in line for line in lines)
    if not info.pulses:
        info.notes.append("no scanner pulses: onsets from the first event")
    if info.task is not None:
        r_columns = R_COLUMN_COUNTS.get(info.task.task)
        if r_columns and len(info.columns) != r_columns:
            info.notes.append(f"{len(info.columns)} columns (the R script expects {r_columns})")
        try:
            table = info.task.extract(lines)
        except (ValueError, KeyError, IndexError) as e:
            info.state = UNREADABLE
            info.notes.append(f"can't extract events: {e!r}")
            return info
        info.trial_types = Counter(table.trial_type)
        info.events_end_s = max(
            (
                onset + (duration or 0.0)
                for onset, duration in zip(table.onset, table.duration, strict=True)
            ),
            default=None,
        )
        if table.trial_type[-1] != "thankyou":
            info.state = ABORTED
            info.notes.append(f"ends at '{table.trial_type[-1]}', before the thank-you screen")
    return info


def log_record(info: LogInfo, stamp: list[int]) -> dict[str, Any]:
    """What inspect_log read from the file (not the corrections), for the scan cache."""
    return {
        "stamp": stamp,
        "found_date": info.found_date.isoformat() if info.found_date else None,
        "date_source": info.date_source,
        "state": info.state,
        "notes": info.notes,
        "columns": list(info.columns),
        "pulses": info.pulses,
        "trial_types": dict(info.trial_types),
        "events_end_s": info.events_end_s,
    }


def log_from_record(
    log_file: Path, root: Path, record: dict[str, Any], correction: Correction | None
) -> LogInfo:
    found = record["found_date"]
    return LogInfo(
        path=log_file,
        key=log_file.relative_to(root).as_posix(),
        task=task_for_log(log_file, root),
        file_subject=log_subject_id(log_file),
        found_date=dt.date.fromisoformat(found) if found else None,
        date_source=record["date_source"],
        correction=correction or Correction(),
        state=record["state"],
        notes=list(record["notes"]),
        columns=tuple(record["columns"]),
        pulses=record["pulses"],
        trial_types=Counter(record["trial_types"]),
        events_end_s=record["events_end_s"],
    )


def inspect_logs(
    behav_root: Path,
    corrections: dict[str, Correction],
    progress: Callable[[int, int], None] | None = None,
    cache: dict[str, Any] | None = None,
) -> list[LogInfo]:
    """Every .log below behav_root, with its corrections applied. progress, if given, is
    called with (logs read, total) as it goes.

    cache (ScanCache.logs), if given: a log whose size and modification time are unchanged is
    taken from it instead of being read; it is updated to the logs found now. Unreadable logs
    aren't kept, so a file that was offline (e.g. a paused OneDrive) is tried again."""
    infos = []
    fresh: dict[str, Any] = {}
    logs = list(find_logs(behav_root))
    for n, log_file in enumerate(logs, start=1):
        key = log_file.relative_to(behav_root).as_posix()
        try:
            stamp = file_stamp(log_file) if cache is not None else None
        except OSError:
            stamp = None
        record = cache.get(key) if cache is not None and stamp is not None else None
        if record is not None and record.get("stamp") == stamp:
            info = log_from_record(log_file, behav_root, record, corrections.get(key))
        else:
            info = inspect_log(log_file, behav_root, corrections.get(key))
        if stamp is not None and info.state != UNREADABLE:
            fresh[key] = log_record(info, stamp)
        infos.append(info)
        if progress is not None:
            progress(n, len(logs))
    if cache is not None:
        cache.clear()
        cache.update(fresh)
    return infos


def place_logs(log_dates: list[dt.date | None], session_dates: list[dt.date]) -> dict[int, int]:
    """Which logs go straight into which session: {log index: session index}.

    A log is placed when its date matches exactly one session and no other log matches that
    session. A single log and a single session belong together even when the dates differ
    (the crosscheck flags that) -- if there is no other session, it is that one.
    """
    claims: dict[int, list[int]] = {}
    for i, log_date in enumerate(log_dates):
        same_day = [
            s for s, d in enumerate(session_dates) if log_date is not None and d == log_date
        ]
        if len(same_day) == 1:
            claims.setdefault(same_day[0], []).append(i)
    placed = {logs[0]: s for s, logs in claims.items() if len(logs) == 1}
    if not placed and len(log_dates) == 1 and len(session_dates) == 1:
        placed = {0: 0}
    return placed


@dataclass(frozen=True)
class SessionInfo:
    subject: str
    label: str  # BIDS session label, "1", "2", ...
    date: dt.date
    tasks: frozenset[str]  # BIDS task labels with a scan in this session
    folder: str = ""  # DICOM session folder name
    raw_id: str = ""  # ID as written in the folder name
    time: str = ""  # HH:MM the scan started, from the folder name

    @property
    def when(self) -> str:
        return f"{self.date:%Y-%m-%d} {self.time}".strip()

    @property
    def name(self) -> str:
        return f"sub-{self.subject}_ses-{self.label}"


@dataclass
class LogMatch:
    info: LogInfo
    status: str
    session: SessionInfo | None = None
    hint: str = ""


def _dates(sessions: list[SessionInfo]) -> str:
    return ", ".join(f"ses-{s.label} {s.when}" for s in sessions)


def match_logs(infos: list[LogInfo], sessions: list[SessionInfo]) -> list[LogMatch]:
    """Each log's place among the scan sessions (same rule the converter uses)."""
    by_subject: dict[str, list[SessionInfo]] = {}
    for s in sessions:
        by_subject.setdefault(s.subject, []).append(s)

    groups: dict[tuple[str, str], list[LogInfo]] = {}
    matches: dict[int, LogMatch] = {}
    for info in infos:
        if info.task is None:
            matches[id(info)] = LogMatch(
                info, NO_TASK, hint="no cogreappraisal/emolabeling in path"
            )
        elif info.excluded:
            matches[id(info)] = LogMatch(info, EXCLUDED, hint=f"marked '{info.correction.review}'")
        else:
            groups.setdefault((info.subject, info.task.task), []).append(info)

    for (subject, task), logs in groups.items():
        own = by_subject.get(subject, [])
        with_task = [s for s in own if task in s.tasks]
        placed = place_logs([i.date for i in logs], [s.date for s in with_task])
        for n, info in enumerate(logs):
            if n in placed:
                session = with_task[placed[n]]
                differs = info.date is not None and info.date != session.date
                hint = f"dates differ: log {info.date}, scan {session.when}" if differs else ""
                matches[id(info)] = LogMatch(info, MATCHED, session, hint)
            elif not own:
                matches[id(info)] = LogMatch(info, UNKNOWN_ID)
            elif info.date is None:
                matches[id(info)] = LogMatch(info, NO_DATE, hint=f"scans: {_dates(own)}")
            elif any(s.date == info.date for s in with_task):
                session = next(s for s in with_task if s.date == info.date)
                matches[id(info)] = LogMatch(info, DUPLICATE, session, "same day: pick one")
            elif any(s.date == info.date for s in own):
                session = next(s for s in own if s.date == info.date)
                matches[id(info)] = LogMatch(info, NO_SCAN, session, f"no {task} scan that day")
            else:
                matches[id(info)] = LogMatch(info, DATE_MISMATCH, hint=f"scans: {_dates(own)}")

    # Wrong ID? Suggest same-day sessions with this task's scan that have no log of their own.
    claimed = {
        (m.session.name, m.info.task.task)
        for m in matches.values()
        if m.status == MATCHED and m.session is not None and m.info.task is not None
    }
    for m in matches.values():
        if m.status != UNKNOWN_ID or m.info.task is None:
            continue
        task = m.info.task.task
        candidates = [
            s
            for s in sessions
            if s.date == m.info.date and task in s.tasks and (s.name, task) not in claimed
        ]
        m.hint = (
            "same day, no log yet: "
            + ", ".join(f"sub-{s.subject}" + (f" ({s.time})" if s.time else "") for s in candidates)
            if candidates
            else "no scan session has this ID"
        )
    return [matches[id(info)] for info in infos]


def needs_review(m: LogMatch) -> bool:
    """Something to look at, with no verdict and not marked crosschecked yet."""
    if m.info.correction.review or m.info.correction.crosschecked:
        return False
    return m.status != MATCHED or m.info.state != COMPLETE or bool(m.info.notes)


def same_day_rivals(matches: list[LogMatch], chosen: LogMatch) -> list[LogMatch]:
    """The other logs competing with `chosen` for one session + task (same subject and day)."""
    if chosen.info.task is None or chosen.info.date is None:
        return []
    return [
        m
        for m in matches
        if m is not chosen
        and m.info.task is not None
        and m.info.task.task == chosen.info.task.task
        and m.info.subject == chosen.info.subject
        and m.info.date == chosen.info.date
    ]


ORDINAL_MODULATORS = {"rating"}  # integer ratings: ordered categories, no means


def _number(value: str | float | None) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return None if math.isnan(number) else number


def _value_text(value: float) -> str:
    return f"{round(value, 2):g}"  # 3, 2.5, 0.67


def events_summary(table: EventTable) -> list[str]:
    """Plain-text summary of one log's events: trial types, and the modulator per trial type.

    Ratings are ordinal, so they're described by how often each value occurs, the median
    and the range -- never a mean. Block accuracy (a proportion) gets median and range.
    Trial types that never carry a modulator value (fixation, thank-you) are skipped there.
    """
    total = sum(d for d in table.duration if d is not None)
    lines = [f"{len(table.trial_type)} events, {total:.1f} s in all"]
    durations: dict[str, list[float]] = {}
    for trial_type, duration in zip(table.trial_type, table.duration, strict=True):
        durations.setdefault(trial_type, []).append(duration or 0.0)
    for trial_type, spent in durations.items():
        lines.append(f"  {trial_type}: {len(spent)} event(s), {sum(spent):.1f} s")

    values: dict[str, list[float]] = {}
    missing: Counter[str] = Counter()
    for trial_type, raw in zip(table.trial_type, table.modulator_values, strict=True):
        number = _number(raw)
        if number is None:
            missing[trial_type] += 1
        else:
            values.setdefault(trial_type, []).append(number)
    ordinal = table.modulator in ORDINAL_MODULATORS
    kind = "ordinal: counts, median, range" if ordinal else "median, range"
    lines.append(f"{table.modulator} ({kind}):")
    if not values:
        lines.append("  no values")
        return lines
    groups = list(values.items())
    if len(groups) > 1:
        groups.append(("all", [v for vs in values.values() for v in vs]))
    for trial_type, numbers in groups:
        # Only trial types that carry values elsewhere count as missing (not fixations).
        n_missing = sum(missing[t] for t in values) if trial_type == "all" else missing[trial_type]
        text = (
            f"  {trial_type}: n={len(numbers)}, median {_value_text(statistics.median(numbers))}, "
            f"range {_value_text(min(numbers))}-{_value_text(max(numbers))}"
        )
        if ordinal:
            counts = Counter(numbers)
            text += "; " + " ".join(f"[{_value_text(v)}]x{counts[v]}" for v in sorted(counts))
        if n_missing:
            text += f"; {n_missing} without a value"
        lines.append(text)
    return lines


@dataclass
class TaskInventory:
    logs: int = 0
    layouts: Counter[tuple[str, ...]] = field(default_factory=Counter)  # column headers
    trial_types: Counter[str] = field(default_factory=Counter)  # logs containing each type
    states: Counter[str] = field(default_factory=Counter)


def inventory(infos: list[LogInfo]) -> dict[str, TaskInventory]:
    """Per task: how many logs, their column layouts, trial types and completeness."""
    result: dict[str, TaskInventory] = {}
    for info in infos:
        name = info.task.task if info.task else "(unknown task)"
        inv = result.setdefault(name, TaskInventory())
        inv.logs += 1
        inv.states[info.state] += 1
        if info.columns:
            inv.layouts[info.columns] += 1
        inv.trial_types.update(set(info.trial_types))
    return dict(sorted(result.items()))
