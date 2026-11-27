"""The study's scan inventory (the follow-up spreadsheet, saved as CSV) next to the BIDS folder.

One row per scan visit: the participant ID, the scan date, how many runs of each scan were
done and the comments on the scan. The sheet goes in the BIDS folder as
code/scan_inventory.csv (Excel: File > Save As > CSV), so it travels with the data: chosen in
the toolbox, it is checked and copied there. It is only read: each row is paired with its BIDS
session by ID and date, so the comments show beside the session and its behaviour logs, and
the run counts are compared with the runs converted.

Columns are found by name, ignoring case and extra spaces. DTI isn't converted, so its run
counts aren't compared.
"""

import csv
import datetime as dt
import logging
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from fmri_local_process.processing.action_log import record_action
from fmri_local_process.processing.behav_logs import canonical_id

log = logging.getLogger(__name__)

INVENTORY_FILE = Path("code") / "scan_inventory.csv"
ID_COLUMN = "participant id #"
DATE_COLUMN = "scan date"
COMMENT_COLUMN = "comments on scan"
# The sheet's "number of runs" columns, and the BIDS scan each counts.
RUN_COLUMNS = {
    "t1- number of runs": "T1w",
    "peer - number of runs": "task-Peer_bold",
    "resting state - number of runs": "task-Rest_bold",
    "cog reappraisal - number of runs": "task-Reappraisal_bold",
    "emotional la - number of runs": "task-Matching_bold",
}
# Day before month, as dates are written in South Africa; ISO first.
DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d %H:%M:%S",
)  # fmt: skip
LEADING_NUMBER = re.compile(r"^\s*(\d+)")
ENCODINGS = ("utf-8-sig", "cp1252")  # Excel saves "CSV UTF-8" or the Windows code page
ZIP_SIGNATURE = b"PK\x03\x04"  # an .xlsx is a zip file


class InventoryError(Exception):
    """The inventory file can't be read, or lacks a column it needs."""


@dataclass(frozen=True)
class InventoryRow:
    line: int  # row number in the spreadsheet (the header is row 1)
    raw_id: str  # as written in the sheet
    subject: str  # as BIDS uses it (twin 12345-1 -> 12345T1)
    date: dt.date | None
    runs: dict[str, int] = field(default_factory=dict)  # BIDS scan label -> runs done
    comment: str = ""

    @property
    def where(self) -> str:
        """'row 5 (12345, 2025-01-24)'"""
        return f"row {self.line} ({self.raw_id}, {self.date or 'no date'})"


@dataclass(frozen=True)
class InventoryMatch:
    row: InventoryRow
    date_differs: bool = False  # the subject's only session, scanned on another day


def header_key(name: str) -> str:
    return " ".join(name.split()).lower()


def parse_date(text: str) -> dt.date | None:
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_runs(text: str) -> int | None:
    """'2' or '2 (first aborted)' -> 2; '', 'n/a' -> None (not compared)."""
    match = LEADING_NUMBER.match(text)
    return int(match.group(1)) if match else None


def read_text(path: Path) -> str:
    with path.open("rb") as f:
        if f.read(len(ZIP_SIGNATURE)) == ZIP_SIGNATURE:
            raise InventoryError(
                f"{path.name} is an Excel workbook renamed to .csv: open it in Excel and "
                "save it as CSV (File > Save As > CSV UTF-8)"
            )
    for encoding in ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise InventoryError(f"{path.name}: not a text file (save the sheet as CSV)")


def read_inventory(bids_root: Path) -> tuple[list[InventoryRow], list[str]]:
    """The BIDS folder's inventory rows and what's odd about it (rows without a readable
    date, ...). ([], []) when it has none; InventoryError when it can't be used."""
    try:
        return read_inventory_file(bids_root / INVENTORY_FILE)
    except FileNotFoundError:
        return [], []


def read_inventory_file(path: Path) -> tuple[list[InventoryRow], list[str]]:
    """An inventory CSV's rows and what's odd about it. FileNotFoundError when it isn't
    there, InventoryError when it can't be used."""
    try:
        text = read_text(path)
    except FileNotFoundError:
        raise
    except OSError as e:
        raise InventoryError(f"Can't read {path.name}: {e}") from e
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(text.splitlines(), dialect)
    header = [header_key(h) for h in next(reader, [])]
    missing = [name for name in (ID_COLUMN, DATE_COLUMN) if name not in header]
    if missing:
        raise InventoryError(f"{path.name} has no column {', '.join(map(repr, missing))}")
    column = {name: i for i, name in reversed(list(enumerate(header)))}  # first one wins
    rows, problems = [], []
    for line, cells in enumerate(reader, start=2):

        def value(name: str, cells: list[str] = cells) -> str:
            i = column.get(name)
            return cells[i].strip() if i is not None and i < len(cells) else ""

        raw_id = value(ID_COLUMN)
        if not raw_id:
            continue  # a blank or notes-only row
        date_text = value(DATE_COLUMN)
        date = parse_date(date_text)
        if date is None:
            problems.append(f"Scan inventory row {line} ({raw_id}): date {date_text!r} not read")
        runs = {
            label: n
            for name, label in RUN_COLUMNS.items()
            if (n := parse_runs(value(name))) is not None
        }
        row = InventoryRow(line, raw_id, canonical_id(raw_id), date, runs, value(COMMENT_COLUMN))
        rows.append(row)
    return rows, problems


def import_inventory(bids_root: Path, source: Path) -> int:
    """Check the CSV at source and copy it in as the BIDS folder's inventory, replacing any
    earlier one; logged. The number of rows read; InventoryError if it can't be used."""
    try:
        rows, _ = read_inventory_file(source)
    except FileNotFoundError as e:
        raise InventoryError(f"No file {source}") from e
    if not rows:
        raise InventoryError(f"{source.name} has no rows with a participant ID")
    target = bids_root / INVENTORY_FILE
    if source.resolve() == target.resolve():
        return len(rows)  # chose the one already in use
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".tmp")
    shutil.copyfile(source, partial)
    partial.replace(target)  # never half a file in use
    record_action(bids_root, "scan inventory", INVENTORY_FILE.as_posix(), f"{len(rows)} rows")
    return len(rows)


def match_inventory(
    rows: list[InventoryRow], sessions: Mapping[str, tuple[str, dt.date | None]]
) -> dict[str, InventoryMatch]:
    """{session name: its inventory row}, for sessions = {name: (subject, scan date)}.

    A row goes with the session of the same subject and date. A subject with one session and
    one row is paired even when the dates differ (as the converter does with a log), and the
    match says so."""
    by_day: dict[tuple[str, dt.date | None], list[str]] = {}
    per_subject: dict[str, list[str]] = {}
    for name, (subject, day) in sessions.items():
        by_day.setdefault((subject, day), []).append(name)
        per_subject.setdefault(subject, []).append(name)
    rows_per_subject: dict[str, int] = {}
    for row in rows:
        rows_per_subject[row.subject] = rows_per_subject.get(row.subject, 0) + 1
    matches: dict[str, InventoryMatch] = {}
    for row in rows:
        same_day = by_day.get((row.subject, row.date), []) if row.date else []
        if len(same_day) == 1 and same_day[0] not in matches:
            matches[same_day[0]] = InventoryMatch(row)
            continue
        only = per_subject.get(row.subject, [])
        if not same_day and len(only) == 1 and rows_per_subject[row.subject] == 1:
            matches[only[0]] = InventoryMatch(row, date_differs=True)
    return matches


def run_differences(
    row: InventoryRow, converted: dict[str, int], kept: set[str]
) -> list[tuple[str, int, int]]:
    """(scan, runs in the sheet, runs in BIDS) where they differ. A scan whose run was chosen
    at crosscheck (kept) has one run left by design, so it only differs if it has none."""
    differences = []
    for label, done in sorted(row.runs.items()):
        found = converted.get(label, 0)
        if found == done or (label in kept and found == 1 and done >= 1):
            continue
        differences.append((label, done, found))
    return differences
