"""Philani DICOM -> BIDS conversion.

Python port of philani_scripts/philaniToBIDS.sh (master). Input is a folder of DICOM session
folders named ``<YYYYMMDD>_<x>_<subject id>[...]``. Every session is converted, as
``sub-<id>/ses-<n>/`` with n = 1, 2, ... in calendar order of the scan date.

Everything is converted straight into its session; the human then chooses at crosscheck:

* a scan with several series in one session -> every series as ``..._run-<n>_...`` in that
  session; keeping one at crosscheck gives it the run-less name and deletes the others
* a behavioural log goes into the session its date picks out (if no other log claims it), or
  -- when the subject has a single session with that task and a single log -- into that one
  (a date that differs is flagged at crosscheck). Logs that fit nowhere are not converted but
  listed (report.unplaced), to sort out in the crosscheck's "IDs & behaviour logs"

Each subject's ``sub-<id>_sessions.tsv`` records which DICOM folder (and scan date/time) each
``ses-<n>`` came from. When an earlier session turns up later, the existing session folders
are renamed to keep ses-1, ses-2, ... in date order.

Corrected IDs/dates from the crosscheck (code/behav_corrections.json,
code/dicom_id_corrections.json) are applied first; the raw files are never renamed.

A series with truncated DICOM files (an incomplete copy) is converted from its intact files
and listed in the report; nothing is written about it into the BIDS folder; the crosscheck
GUI spots the short scan itself. Delete that output and re-run once the raw data is complete.

Each series is converted (and its T1w defaced) in a temporary folder, ``.converting/`` in the
BIDS folder, and only moved into its session once that has all worked; the temporary folder
is then removed. A conversion that stops halfway (crash, power cut, a full disk) never leaves
a half-written scan, or an un-defaced T1w, in the session. Before each session the free disk
space is checked against the size of its DICOM folder.

A DICOM session deleted at crosscheck (a dud visit) is listed in
code/excluded_sessions.json and left out from then on, so the next conversion doesn't bring
it back; the remaining sessions are renumbered.
"""

import csv
import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from fnmatch import fnmatchcase
from pathlib import Path

from fmri_local_process.processing.action_log import record_action
from fmri_local_process.processing.behav_logs import (
    BEHAV_TASKS,
    BehavTask,
    canonical_id,
    id_problem,
    read_log_lines,
    write_events_tsv,
)
from fmri_local_process.processing.behav_match import (
    LogInfo,
    SessionInfo,
    inspect_logs,
    place_logs,
    read_corrections,
)
from fmri_local_process.processing.deface import DefaceFn
from fmri_local_process.processing.records import write_json_atomic
from fmri_local_process.processing.session_sync import read_sessions_tsv, sessions_tsv

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]
"""(steps done, total steps, what is starting now) -- e.g. to drive a progress bar"""

ConvertFn = Callable[[Path, Path, str], None]
"""(dicom series folder, output folder, file stem) -> writes <stem>.nii.gz + <stem>.json"""

# Followed by the tool's name. Older sidecars say "... using FreeSurfer mri_deface".
DEFACE_NOTE = "The T1w image was defaced using"
DATE_FORMAT = "%Y%m%d"  # date at the start of each DICOM session folder name
SKIP_SESSION_ABOVE_BYTES = 1024**3  # an existing ses-* folder over 1 GB counts as done
IGNORED_FOLDERS = {"trash"}
PENDING_DIR = "crosscheck_pending"
DICOM_CORRECTIONS_FILE = Path("code") / "dicom_id_corrections.json"
DICOM_DATE_CORRECTIONS_FILE = Path("code") / "dicom_date_corrections.json"
DICOM_SESSION_CORRECTIONS_FILE = Path("code") / "dicom_session_corrections.json"
EXCLUDED_SESSIONS_FILE = Path("code") / "excluded_sessions.json"
CONVERTING_DIR = ".converting"  # in the BIDS folder: series being converted right now
SPACE_MARGIN_BYTES = 1024**3  # free space to keep on top of a session's DICOM size
SESSIONS_COLUMNS = ("session_id", "acq_time", "source_folder")
RESET_FILES = ("crosscheck.json", "selected_runs.json")
TRUNCATED_MESSAGE = "File not large enough"  # dcm2niix's complaint about a cut-off file
# Files of one series hold one slice/volume each, so they are within a few bytes of each
# other; one well below the largest was cut off mid-copy.
TRUNCATED_FRACTION = 0.9

DATASET_DESCRIPTION = {
    "BIDSVersion": "1.9.0",
    "License": "Stellenbosch/Columbia University",
    "Name": "Resilient Emotion Regulation Development in a South African Birth Cohort",
    "ReferencesAndLinks": ["References", "Links"],
    "Authors": ["Philani Study Team"],
    "HowToAcknowledge": "Please cite this dataset if used in publications",
    "Funding": ["Funding information"],
    "EthicsApprovals": ["Ethics approval information"],
}


@dataclass(frozen=True)
class ScanRule:
    pattern: str  # matched case-sensitively against each folder's full path, like `find -wholename`
    datatype: str  # anat / func
    entities: str  # BIDS entities between ses-<n> and run-<n>, e.g. "task-Rest"
    suffix: str  # BIDS suffix, e.g. "bold"

    @property
    def task_name(self) -> str | None:
        return self.entities.removeprefix("task-") if self.entities.startswith("task-") else None

    @property
    def scan(self) -> str:
        """The BIDS scan it makes, as written in the rules editor: "T1w", "task-Rest_bold"."""
        return "_".join(p for p in (self.entities, self.suffix) if p)

    @property
    def output(self) -> tuple[str, str, str]:
        """Rules with the same output feed one scan (several matches: runs to pick from)."""
        return (self.datatype, self.entities, self.suffix)


# The Philani protocol's series names (philaniToBIDS.sh), plus a clinical scan's T1.
SCAN_RULES = (
    ScanRule("*_ABCD_T1w_*_RMS", "anat", "", "T1w"),
    ScanRule("*_Cog_reappraisal_task_AP", "func", "task-Reappraisal", "bold"),
    ScanRule("*_labeling_task_AP", "func", "task-Matching", "bold"),
    ScanRule("*_rest_AP", "func", "task-Rest", "bold"),
    ScanRule("*_AP_Peer", "func", "task-Peer", "bold"),
    ScanRule("*_Checkerboard_task_AP", "func", "task-Checkerboard", "bold"),
    ScanRule("*t1_mprage*", "anat", "", "T1w"),
)
SCAN_RULES_FILE = Path("code") / "scan_rules.json"
ANAT_SUFFIXES = ("T1w", "T2w", "FLAIR")
TASK_SCAN = re.compile(r"task-([A-Za-z0-9]+)_bold")


def rule_from(pattern: str, scan: str) -> ScanRule:
    """A rule from the editor's two fields; ValueError says what is wrong."""
    pattern, scan = pattern.strip(), scan.strip()
    if not pattern:
        raise ValueError("a folder pattern is needed (e.g. *_rest_AP)")
    if scan in ANAT_SUFFIXES:
        return ScanRule(pattern, "anat", "", scan)
    if match := TASK_SCAN.fullmatch(scan):
        return ScanRule(pattern, "func", f"task-{match.group(1)}", "bold")
    raise ValueError(
        f"{scan!r} isn't a BIDS scan this converter writes: use one of "
        f"{', '.join(ANAT_SUFFIXES)}, or task-<Name>_bold (letters and digits only)"
    )


def read_scan_rules(bids_root: Path | None) -> tuple[ScanRule, ...]:
    """The scan rules saved with a BIDS folder (code/scan_rules.json), else SCAN_RULES."""
    if bids_root is None:
        return SCAN_RULES
    try:
        saved = json.loads((bids_root / SCAN_RULES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SCAN_RULES
    rules = []
    for entry in saved.get("rules", []) if isinstance(saved, dict) else []:
        try:
            rules.append(rule_from(str(entry.get("pattern", "")), str(entry.get("scan", ""))))
        except (ValueError, AttributeError) as e:
            log.warning("Ignoring a scan rule in %s: %s", SCAN_RULES_FILE, e)
    return tuple(rules)


def write_scan_rules(bids_root: Path, rules: tuple[ScanRule, ...]) -> None:
    old = {(r.pattern, r.scan) for r in read_scan_rules(bids_root)}
    new = {(r.pattern, r.scan) for r in rules}
    write_json_atomic(
        bids_root / SCAN_RULES_FILE,
        {"rules": [{"pattern": r.pattern, "scan": r.scan} for r in rules]},
    )
    for pattern, scan in sorted(new - old):
        record_action(bids_root, "add scan rule", pattern, scan)
    for pattern, scan in sorted(old - new):
        record_action(bids_root, "remove scan rule", pattern, scan)


@dataclass(frozen=True)
class DicomSession:
    folder: Path
    date: date
    subject_id: str  # canonical (and corrected, if a correction was recorded)
    raw_id: str = ""  # as written in the folder name
    time: str = ""  # HH:MM from the folder name, when it has one


@dataclass(frozen=True)
class BidsSession:
    dicom: DicomSession
    label: str  # "1", "2", ... in date order

    @property
    def subject_id(self) -> str:
        return self.dicom.subject_id

    @property
    def rel_dir(self) -> Path:
        return Path(f"sub-{self.subject_id}") / f"ses-{self.label}"

    @property
    def prefix(self) -> str:
        return f"sub-{self.subject_id}_ses-{self.label}"


@dataclass
class ConversionReport:
    converted: list[str] = field(default_factory=list)
    to_pick: list[str] = field(default_factory=list)  # repeated runs: choose one at crosscheck
    unplaced: list[tuple[str, str]] = field(default_factory=list)  # (log, why not converted)
    renumbered: list[str] = field(default_factory=list)  # e.g. "sub-A: ses-1 -> ses-2"
    flagged: list[tuple[str, str]] = field(default_factory=list)  # converted, but incomplete
    missing: list[str] = field(default_factory=list)  # expected behavioural logs not found
    skipped: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)  # DICOM folders deleted at crosscheck
    # (DICOM folder, its series folders): sessions where no series matched a scan rule
    no_scans: list[tuple[str, str]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (what, why)


class NotEnoughSpaceError(OSError):
    """Too little free disk space to convert the next session: conversion stops there."""


def bids_stem(prefix: str, entities: str, suffix: str, run: int | None = None) -> str:
    parts = [prefix]
    if entities:
        parts.append(entities)
    if run is not None:
        parts.append(f"run-{run}")
    parts.append(suffix)
    return "_".join(parts)


def read_dicom_corrections(bids_root: Path) -> dict[str, str]:
    """Subject IDs the human corrected at crosscheck: {DICOM session folder name: ID}."""
    try:
        saved = json.loads((bids_root / DICOM_CORRECTIONS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in saved.items() if str(v).strip()}


def write_dicom_corrections(bids_root: Path, corrections: dict[str, str]) -> None:
    write_folder_map(
        bids_root, DICOM_CORRECTIONS_FILE, corrections, "correct DICOM folder ID", "(folder name)"
    )


def read_folder_map(bids_root: Path, file: Path) -> dict[str, str]:
    """A {DICOM folder name: value} correction file ({} if there is none)."""
    try:
        saved = json.loads((bids_root / file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in saved.items() if str(v).strip()}


def write_folder_map(
    bids_root: Path, file: Path, values: dict[str, str], action: str, unset: str
) -> None:
    """Save a {DICOM folder name: value} correction file; log each change."""
    old = read_folder_map(bids_root, file)
    kept = {k: v.strip() for k, v in sorted(values.items()) if v.strip()}
    write_json_atomic(bids_root / file, kept)
    for folder in sorted(old.keys() | kept.keys()):
        if old.get(folder) != kept.get(folder):
            detail = f"{old.get(folder, unset)} -> {kept.get(folder, unset)}"
            record_action(bids_root, action, folder, detail)


def read_dicom_date_corrections(bids_root: Path) -> dict[str, date]:
    """Scan dates the human corrected: {DICOM session folder name: date}."""
    dates = {}
    for folder, text in read_folder_map(bids_root, DICOM_DATE_CORRECTIONS_FILE).items():
        try:
            dates[folder] = date.fromisoformat(text)
        except ValueError:
            log.warning("Ignoring the corrected date %r of %s: not YYYY-MM-DD", text, folder)
    return dates


def write_dicom_date_corrections(bids_root: Path, dates: dict[str, date]) -> None:
    values = {folder: day.isoformat() for folder, day in dates.items()}
    write_folder_map(
        bids_root, DICOM_DATE_CORRECTIONS_FILE, values, "correct DICOM folder date", "(folder name)"
    )


def read_dicom_session_corrections(bids_root: Path) -> dict[str, str]:
    """Session numbers the human set: {DICOM session folder name: label, e.g. "2"}."""
    return read_folder_map(bids_root, DICOM_SESSION_CORRECTIONS_FILE)


def write_dicom_session_corrections(bids_root: Path, labels: dict[str, str]) -> None:
    write_folder_map(
        bids_root, DICOM_SESSION_CORRECTIONS_FILE, labels, "set DICOM folder session", "(by date)"
    )


def read_excluded_sessions(bids_root: Path) -> dict[str, str]:
    """DICOM session folders deleted at crosscheck: {folder name: why}. Never converted."""
    try:
        saved = json.loads((bids_root / EXCLUDED_SESSIONS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in saved.items()}


def exclude_session(bids_root: Path, folder_name: str, why: str) -> None:
    """Leave DICOM session folder_name out of every conversion from now on."""
    excluded = read_excluded_sessions(bids_root) | {folder_name: why}
    path = bids_root / EXCLUDED_SESSIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, dict(sorted(excluded.items())))


def parse_session_folder(
    folder: Path, corrected_id: str | None = None, corrected_date: date | None = None
) -> DicomSession | None:
    """<YYYYMMDD>_<HHMM or x>_<subject id>[...] -> DicomSession; None if it doesn't fit.

    The ID is made canonical (twin 12345-1 -> 12345T1, other non-alphanumerics dropped); a
    corrected_id or corrected_date recorded at crosscheck replaces the folder's own. With
    both, the folder's name doesn't matter at all (e.g. "Clinical Scan - 28th October 2026").
    """
    parts = folder.name.split("_")
    if len(parts) < 3:
        if not (corrected_id and corrected_date):
            return None
        parts = ["", "", ""]  # nothing usable in the name: the corrections say it all
    try:
        scan_date = datetime.strptime(parts[0], DATE_FORMAT).date()
    except ValueError:
        if corrected_date is None:
            return None
        scan_date = corrected_date
    if corrected_date is not None:
        log.info("%s: using corrected scan date %s", folder.name, corrected_date)
        scan_date = corrected_date
    raw_id = parts[2]
    subject_id = canonical_id(corrected_id or raw_id)
    if not subject_id:
        return None
    if corrected_id:
        log.info("%s: using corrected subject id %r", folder.name, subject_id)
    elif problem := id_problem(raw_id):
        log.warning("%s: subject id %r -> %r (%s)", folder.name, raw_id, subject_id, problem)
    time = f"{parts[1][:2]}:{parts[1][2:]}" if len(parts[1]) == 4 and parts[1].isdigit() else ""
    return DicomSession(folder, scan_date, subject_id, raw_id, time)


def single_session(folder: Path, subject_id: str) -> DicomSession:
    """Treat folder as one session of subject_id, whatever its name (--subject).

    The date comes from the folder name when it has one; it only orders sessions, and a
    single session is always ses-1.
    """
    label = canonical_id(subject_id)
    if not label:
        raise ValueError(f"subject id {subject_id!r} has no letters or digits")
    parsed = parse_session_folder(folder)
    return DicomSession(folder, parsed.date if parsed else date.min, label, subject_id)


def find_sessions(
    dicom_root: Path,
    corrections: dict[str, str] | None = None,
    excluded: Collection[str] = (),
    dates: dict[str, date] | None = None,
) -> list[DicomSession]:
    """Every session folder in dicom_root, with crosscheck ID and date corrections applied,
    except the excluded ones (deleted at crosscheck)."""
    sessions = []
    for folder in sorted(dicom_root.iterdir()):
        if folder.name in IGNORED_FOLDERS or not folder.is_dir():
            continue
        if folder.name in excluded:
            log.info("Leaving out %s: deleted at crosscheck", folder.name)
            continue
        if not any(folder.iterdir()):
            log.warning("Skipping empty folder %s", folder.name)
            continue
        session = parse_session_folder(
            folder, (corrections or {}).get(folder.name), (dates or {}).get(folder.name)
        )
        if session is None:
            log.warning(
                "Skipping %s: name isn't <YYYYMMDD>_<x>_<subject id>; give it a Corrected ID "
                "and Corrected date (ID & Behaviour Logs tab, Scan sessions) to convert it",
                folder.name,
            )
            continue
        sessions.append(session)
    return sessions


def number_sessions(
    sessions: list[DicomSession], fixed: dict[str, str] | None = None
) -> dict[str, list[BidsSession]]:
    """Per subject, label sessions 1, 2, ... in calendar order (folder name breaks ties).

    Labels follow the sessions present now: a newly added earlier session renumbers the rest.
    A session number the human set ({DICOM folder: label}) wins; the others are numbered
    around it, skipping the numbers set. Two folders of one subject set to the same number:
    the earlier keeps it, the later is numbered by date (and a warning logged).
    """
    fixed = fixed or {}
    by_subject: dict[str, list[DicomSession]] = {}
    for session in sessions:
        by_subject.setdefault(session.subject_id, []).append(session)
    numbered = {}
    for subject_id, group in sorted(by_subject.items()):
        group.sort(key=lambda s: (s.date, s.folder.name))
        labels: dict[Path, str] = {}
        for dicom in group:
            label = fixed.get(dicom.folder.name)
            if label is None:
                continue
            if label in labels.values():
                log.warning("sub-%s: %s is also set to ses-%s; numbered by date instead",
                            subject_id, dicom.folder.name, label)  # fmt: skip
                continue
            labels[dicom.folder] = label
        used, n = set(labels.values()), 0
        for dicom in group:
            if dicom.folder in labels:
                continue
            n += 1
            while str(n) in used:
                n += 1
            labels[dicom.folder] = str(n)
        numbered[subject_id] = [BidsSession(d, labels[d.folder]) for d in group]
    return numbered


def write_dataset_description(bids_root: Path) -> None:
    path = bids_root / "dataset_description.json"
    if path.exists():
        return
    path.write_text(json.dumps(DATASET_DESCRIPTION, indent=2) + "\n", encoding="utf-8")
    log.info("Created %s", path)


def remove_old_staging(bids_root: Path) -> None:
    """Older versions staged candidates in crosscheck_pending/. Everything in it is made from
    raw data and is now converted into the sessions instead, so the folder goes."""
    pending = bids_root / PENDING_DIR
    if pending.is_dir():
        log.warning("Removing the old staging folder %s: its items now go into sessions", pending)
        shutil.rmtree(pending)


def write_sessions_tsv(bids_root: Path, subject_id: str, sessions: list[BidsSession]) -> None:
    """BIDS sessions file: each ses-<n> with its scan date/time and DICOM source folder."""
    path = sessions_tsv(bids_root, subject_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(SESSIONS_COLUMNS)
        for s in sessions:
            when = s.dicom.date.isoformat() + (f"T{s.dicom.time}:00" if s.dicom.time else "")
            writer.writerow([f"ses-{s.label}", when, s.dicom.folder.name])


def renumber_sessions(
    bids_root: Path, subject_id: str, sessions: list[BidsSession], report: ConversionReport
) -> None:
    """Rename converted ses-<n> folders whose number changed (an earlier session turned up).

    Which folder is which comes from sessions.tsv (source_folder); without one, the existing
    folders are taken to be numbered right. Files inside are renamed with the folder.
    """
    subject_dir = bids_root / f"sub-{subject_id}"
    old_labels = {
        row.get("source_folder", ""): row.get("session_id", "").removeprefix("ses-")
        for row in read_sessions_tsv(bids_root, subject_id)
    }
    moves = {
        old: s.label
        for s in sessions
        if (old := old_labels.get(s.dicom.folder.name))
        and old != s.label
        and (subject_dir / f"ses-{old}").is_dir()
    }
    if not moves:
        return
    staging = {old: subject_dir / f"renumbering-ses-{old}" for old in moves}
    for old, temporary in staging.items():  # two steps, so ses-1 <-> ses-2 can swap
        (subject_dir / f"ses-{old}").rename(temporary)
    for old, new in moves.items():
        target = subject_dir / f"ses-{new}"
        if target.exists():  # a folder no sessions.tsv row explains: leave both for a human
            staging[old].rename(subject_dir / f"ses-{old}")
            report.failures.append(
                (f"sub-{subject_id}_ses-{old}", f"can't renumber to ses-{new}: it already exists")
            )
            continue
        staging[old].rename(target)
        old_prefix, new_prefix = f"sub-{subject_id}_ses-{old}_", f"sub-{subject_id}_ses-{new}_"
        for path in sorted(target.rglob(f"{old_prefix}*")):
            path.rename(path.with_name(new_prefix + path.name.removeprefix(old_prefix)))
        log.warning("sub-%s: ses-%s renumbered to ses-%s (a session was added or deleted)",
                    subject_id, old, new)  # fmt: skip
        report.renumbered.append(f"sub-{subject_id}: ses-{old} -> ses-{new}")
        record_action(bids_root, "renumber session", f"sub-{subject_id}", f"ses-{old} -> ses-{new}")


def reset_bids_root(bids_root: Path) -> None:
    """Remove sub-* folders, the crosscheck staging area and crosscheck files; keep the rest."""
    for subject_dir in bids_root.glob("sub-*"):
        if subject_dir.is_dir():
            shutil.rmtree(subject_dir)
    shutil.rmtree(bids_root / PENDING_DIR, ignore_errors=True)
    for name in RESET_FILES:
        (bids_root / name).unlink(missing_ok=True)


def folder_size(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())


def strip_duplicate_suffix(out_dir: Path) -> None:
    """dcm2niix appends 'a' when the target name already exists; fold it back."""
    for pattern, ext in (("*a.nii.gz", ".nii.gz"), ("*a.json", ".json")):
        for path in out_dir.glob(pattern):
            path.replace(path.with_name(path.name.removesuffix("a" + ext) + ext))


def update_json(json_path: Path, **fields: str) -> None:
    sidecar = json.loads(json_path.read_text(encoding="utf-8"))
    sidecar.update(fields)
    json_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")


def is_defaced(json_path: Path) -> bool:
    sidecar = json.loads(json_path.read_text(encoding="utf-8"))
    return DEFACE_NOTE in sidecar.get("Notes", "")


def file_listing(folder: Path) -> tuple[str, ...]:
    """Names of the files directly in folder: identifies a copied series folder.

    Sizes are ignored on purpose: a re-export (e.g. differently compressed) of the same
    images keeps the instance-UID file names but not the byte sizes.
    """
    return tuple(sorted(f.name for f in folder.iterdir() if f.is_file()))


def drop_copies(series: list[Path]) -> list[Path]:
    """Keep one of each set of copied series folders (same folder name and file names).

    Scanner exports often hold a second full copy (e.g. an 'Untitled/' subfolder). Those
    are not repeated runs, so they shouldn't go to crosscheck. The shallowest copy is kept.
    """
    kept: dict[tuple[str, tuple[str, ...]], Path] = {}
    for folder in sorted(series, key=lambda d: (len(d.parts), d.as_posix())):
        key = (folder.name, file_listing(folder))
        if key in kept:
            log.info("Ignoring %s: copy of %s", folder, kept[key])
        else:
            kept[key] = folder
    return sorted(kept.values())


def match_series(
    session_folder: Path, rules: tuple[ScanRule, ...] = SCAN_RULES
) -> dict[ScanRule, list[Path]]:
    """Series folders per BIDS scan (keyed by the first rule making it; only scans found).

    Rules with the same output feed one scan: a session with both a Philani and a clinical
    T1w gets both, as runs to pick from. A folder inside another matching folder (a pattern
    such as *t1_mprage* also matches what lies below the series) isn't a series of its own.
    """
    session_dirs = sorted(d for d in session_folder.rglob("*") if d.is_dir())
    by_output: dict[tuple[str, str, str], tuple[ScanRule, list[Path]]] = {}
    for rule in rules:
        _, found = by_output.setdefault(rule.output, (rule, []))
        found += [
            d for d in session_dirs if fnmatchcase(d.as_posix(), rule.pattern) and d not in found
        ]
    matches = {}
    for first, found in by_output.values():
        outermost = [d for d in found if not any(parent in found for parent in d.parents)]
        if series := drop_copies(outermost):
            matches[first] = series
    return matches


def truncated_dicoms(series_dir: Path) -> list[Path]:
    """Files well below the largest in the series: cut off by an incomplete copy."""
    sizes = {f: f.stat().st_size for f in series_dir.iterdir() if f.is_file()}
    if not sizes:
        return []
    limit = TRUNCATED_FRACTION * max(sizes.values())
    return sorted(f for f, size in sizes.items() if size < limit)


@contextmanager
def series_without(series_dir: Path, left_out: list[Path]) -> Iterator[Path]:
    """A temporary copy of series_dir without left_out (the raw folder is never touched)."""
    with tempfile.TemporaryDirectory(prefix="fmri_intact_series_") as tmp:
        intact_dir = Path(tmp)
        for f in series_dir.iterdir():
            if f.is_file() and f not in left_out:
                shutil.copy2(f, intact_dir / f.name)
        yield intact_dir


def convert_intact(series_dir: Path, out_dir: Path, stem: str, convert: ConvertFn) -> list[str]:
    """Convert series_dir; if dcm2niix rejects truncated files, convert without them.

    Returns the problems to flag for crosscheck (empty when the series was complete).
    """
    try:
        convert(series_dir, out_dir, stem)
        return []
    except subprocess.CalledProcessError as e:
        truncated = truncated_dicoms(series_dir)
        if not truncated or TRUNCATED_MESSAGE not in str(e.output):
            raise
    total = sum(1 for f in series_dir.iterdir() if f.is_file())
    log.warning("%s: %d of %d DICOM files truncated, left out", stem, len(truncated), total)
    with series_without(series_dir, truncated) as intact_dir:
        convert(intact_dir, out_dir, stem)
    return [
        f"Incomplete series: {len(truncated)} of {total} DICOM files in {series_dir.name} are "
        "truncated (incomplete copy?) and were left out. Re-copy the raw data and convert again."
    ]


def convert_session(
    session: BidsSession,
    series_by_rule: dict[ScanRule, list[Path]],
    bids_root: Path,
    convert: ConvertFn,
    deface: DefaceFn | None,
    report: ConversionReport,
) -> None:
    session_dir = bids_root / session.rel_dir
    # A scan deleted at crosscheck (e.g. a short one, to reconvert) must not be skipped.
    expected = [
        session_dir
        / rule.datatype
        / (bids_stem(session.prefix, rule.entities, rule.suffix) + ".nii.gz")
        for rule, series in series_by_rule.items()
        if len(series) == 1
    ]
    if (
        session_dir.is_dir()
        and all(p.exists() for p in expected)
        and folder_size(session_dir) > SKIP_SESSION_ABOVE_BYTES
    ):
        log.info("%s already converted (>1 GB), skipping", session.prefix)
        report.skipped.append(session.prefix)
        return
    check_free_space(bids_root, session.dicom.folder)

    for rule, series in series_by_rule.items():
        final_dir = session_dir / rule.datatype
        final_stem = bids_stem(session.prefix, rule.entities, rule.suffix)
        if len(series) == 1:
            targets = [(series[0], final_dir, final_stem, report.converted)]
        elif (final_dir / f"{final_stem}.nii.gz").exists():
            log.info("%s: run already chosen at crosscheck, not reconverting", final_stem)
            continue
        else:
            log.warning("%s: %d series, converted as run-1.. to pick at crosscheck",
                        final_stem, len(series))  # fmt: skip
            targets = [
                (
                    s,
                    final_dir,
                    bids_stem(session.prefix, rule.entities, rule.suffix, run),
                    report.to_pick,
                )
                for run, s in enumerate(series, start=1)
            ]
        for series_dir, out_dir, stem, bucket in targets:
            try:
                problems = convert_series(
                    series_dir, out_dir, stem, rule, convert, deface, bids_root / CONVERTING_DIR
                )
                bucket.append(stem)
                report.flagged.extend((stem, problem) for problem in problems)
            except (subprocess.CalledProcessError, OSError, ValueError) as e:
                if isinstance(e, NotEnoughSpaceError):
                    raise
                log.error("%s failed: %s", stem, e)
                report.failures.append((stem, str(e)))


def check_free_space(bids_root: Path, dicom_folder: Path) -> None:
    """Stop before a session whose DICOMs (plus a margin) don't fit on the BIDS drive.

    The compressed NIfTIs are smaller than the DICOMs, so this errs on the safe side.
    """
    needed = folder_size(dicom_folder) + SPACE_MARGIN_BYTES
    free = shutil.disk_usage(bids_root).free
    if free < needed:
        raise NotEnoughSpaceError(
            f"Only {free / 1024**3:.1f} GB free on the BIDS drive; converting "
            f"{dicom_folder.name} needs about {needed / 1024**3:.1f} GB. Free some space and "
            "convert again: the sessions already converted are kept."
        )


def finish_series(
    nifti: Path, sidecar: Path, rule: ScanRule, deface: DefaceFn | None, defaced_already: bool
) -> None:
    """Deface a T1w (if not done yet) and note the task name in the sidecar."""
    if rule.suffix == "T1w" and deface is not None and not defaced_already:
        log.info("Defacing %s", nifti.name)
        tool = deface(nifti)
        update_json(sidecar, Notes=f"{DEFACE_NOTE} {tool}")
    if rule.task_name:
        update_json(sidecar, TaskName=rule.task_name)


def convert_series(
    series_dir: Path,
    out_dir: Path,
    stem: str,
    rule: ScanRule,
    convert: ConvertFn,
    deface: DefaceFn | None,
    staging_root: Path,
) -> list[str]:
    """Convert one series; returns problems for the report (e.g. truncated files left out).

    A new conversion happens in a temporary folder under staging_root (on the same drive as
    the BIDS folder) and is moved into out_dir only once dcm2niix and defacing have worked;
    the temporary folder is removed whatever happens.
    """
    nifti = out_dir / f"{stem}.nii.gz"
    sidecar = out_dir / f"{stem}.json"
    if nifti.exists() and sidecar.exists():
        log.info("%s exists, not reconverting", stem)
        finish_series(nifti, sidecar, rule, deface, is_defaced(sidecar))
        return []

    log.info("Converting %s -> %s", series_dir.name, stem)
    staging_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{stem}_", dir=staging_root) as tmp:
        work_dir = Path(tmp)
        problems = convert_intact(series_dir, work_dir, stem, convert)
        strip_duplicate_suffix(work_dir)
        new_nifti, new_sidecar = work_dir / nifti.name, work_dir / sidecar.name
        if not (new_nifti.exists() and new_sidecar.exists()):
            raise FileNotFoundError(f"dcm2niix produced no {nifti.name} / {sidecar.name}")
        finish_series(new_nifti, new_sidecar, rule, deface, defaced_already=False)
        out_dir.mkdir(parents=True, exist_ok=True)
        new_sidecar.replace(sidecar)  # the NIfTI last: a scan counts as done once both exist
        new_nifti.replace(nifti)
    return problems


def remove_staging(bids_root: Path) -> None:
    """Leftovers of a conversion that was killed mid-series: never moved into a session."""
    staging = bids_root / CONVERTING_DIR
    if not staging.is_dir():
        return
    if any(staging.iterdir()):
        log.info("Removing %s (left over from an interrupted conversion)", staging)
    shutil.rmtree(staging, ignore_errors=True)


def convert_behaviour(
    subject_id: str,
    sessions_with_task: list[BidsSession],
    task: BehavTask,
    logs: list[LogInfo],
    bids_root: Path,
    report: ConversionReport,
) -> None:
    """Events for one subject/task, each log into the session place_logs picks for it.

    Every subject is expected to have a log for every task; a missing one is logged and
    counted in report.missing, and conversion carries on. Logs that fit no session are not
    converted but listed in report.unplaced. An events.tsv made from another log (e.g. a
    different one was chosen at crosscheck since) is replaced.
    """
    entities = f"task-{task.task}"
    if not logs:
        log.warning("sub-%s: no %s log found", subject_id, task.log_subdir)
        report.missing.append(f"sub-{subject_id}_{entities}")
        return

    placed = place_logs([i.date for i in logs], [s.dicom.date for s in sessions_with_task])
    for n, info in enumerate(logs):
        if n not in placed:
            why = (
                "no scan session with this subject ID"
                if not sessions_with_task
                else f"not matched to one of {len(sessions_with_task)} {task.task} session(s)"
            )
            log.warning("%s: %s; not converted", info.key, why)
            report.unplaced.append((info.key, why))
            continue
        session = sessions_with_task[placed[n]]
        stem = bids_stem(session.prefix, entities, "events")
        tsv = bids_root / session.rel_dir / "func" / f"{stem}.tsv"
        if tsv.exists() and source_log(tsv) == info.path.name:
            continue
        try:
            write_events_tsv(task.extract(read_log_lines(info.path)), tsv)
        except (OSError, ValueError, KeyError) as e:
            log.error("%s from %s failed: %s", tsv.name, info.path.name, e)
            report.failures.append((tsv.name, f"{info.path.name}: {e!r}"))
            continue
        sidecar: dict[str, str] = {"SourceLog": info.path.name}
        if info.date is not None:  # crosscheck flags a log dated other than its scan
            sidecar["SourceLogDate"] = info.date.isoformat()
        if info.corrected:  # so the BIDS folder shows the raw name was overridden
            sidecar["SourceLogCorrectedID"] = info.subject
            sidecar["SourceLogCorrectedDate"] = info.date.isoformat() if info.date else ""
        tsv.with_suffix(".json").write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
        report.converted.append(tsv.name.removesuffix(".tsv"))


def source_log(events_tsv: Path) -> str:
    """The log an events.tsv was made from (its sidecar's SourceLog), or ""."""
    try:
        sidecar = json.loads(events_tsv.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(sidecar.get("SourceLog", ""))


def remove_stray_logs(bids_root: Path) -> None:
    for log_file in bids_root.rglob("*.log"):
        log_file.unlink()


def convert_dicom_root(
    dicom_root: Path,
    bids_root: Path,
    convert: ConvertFn,
    deface: DefaceFn | None,
    behav_root: Path | None = None,
    subject_id: str | None = None,
    progress: ProgressFn | None = None,
) -> ConversionReport:
    """Convert every session folder in dicom_root.

    With subject_id, dicom_root is itself one session of that subject, whatever its name.
    progress, if given, is called before each step: one per session, plus (with behav_root)
    one per subject for its logs and one for logs of subjects without a session.
    """
    report = ConversionReport()
    write_dataset_description(bids_root)
    remove_old_staging(bids_root)
    remove_staging(bids_root)
    log_groups = find_behav_logs(behav_root, bids_root) if behav_root is not None else {}
    excluded = read_excluded_sessions(bids_root)
    rules = read_scan_rules(bids_root)
    if subject_id is not None:
        found = [single_session(dicom_root, subject_id)]
    else:
        found = find_sessions(
            dicom_root,
            read_dicom_corrections(bids_root),
            excluded,
            read_dicom_date_corrections(bids_root),
        )
        report.excluded = sorted(name for name in excluded if (dicom_root / name).is_dir())
    subjects = number_sessions(found, read_dicom_session_corrections(bids_root))
    total = len(found) + (len(subjects) + 1 if behav_root is not None else 0)
    done = 0

    def step(what: str) -> None:
        nonlocal done
        if progress is not None:
            progress(done, total, what)
        done += 1

    for subject_id, sessions in subjects.items():
        renumber_sessions(bids_root, subject_id, sessions, report)
        series = {s: match_series(s.dicom.folder, rules) for s in sessions}
        for session in sessions:
            step(f"{session.prefix} from {session.dicom.folder.name}")
            log.info("%s from %s", session.prefix, session.dicom.folder.name)
            if not series[session]:
                found = ", ".join(series_folder_names(session.dicom.folder)) or "none"
                log.warning(
                    "%s: no series folder matches a scan rule, so nothing is converted "
                    "(series folders found: %s)", session.dicom.folder.name, found,
                )  # fmt: skip
                report.no_scans.append((session.dicom.folder.name, found))
                continue
            convert_session(session, series[session], bids_root, convert, deface, report)
        write_sessions_tsv(bids_root, subject_id, sessions)
        if behav_root is None:
            continue
        step(f"sub-{subject_id}: behavioural logs")
        for task in BEHAV_TASKS:
            with_task = [s for s in sessions if any(r.task_name == task.task for r in series[s])]
            logs = log_groups.get((subject_id, task.task), [])
            convert_behaviour(subject_id, with_task, task, logs, bids_root, report)

    # Logs for subjects without any DICOM session: not converted, listed in the report.
    if behav_root is not None:
        step("behavioural logs without a scan session")
    tasks = {t.task: t for t in BEHAV_TASKS}
    for (subject_id, task_label), logs in sorted(log_groups.items()):
        if subject_id not in subjects:
            convert_behaviour(subject_id, [], tasks[task_label], logs, bids_root, report)
    remove_stray_logs(bids_root)
    remove_staging(bids_root)
    if progress is not None:
        progress(total, total, "done")
    return report


def find_behav_logs(behav_root: Path, bids_root: Path) -> dict[tuple[str, str], list[LogInfo]]:
    """Every .log below behav_root with its crosscheck corrections applied, grouped by
    (subject, task) -- task from the path, subject from the file name unless corrected."""
    groups: dict[tuple[str, str], list[LogInfo]] = {}
    infos = inspect_logs(behav_root, read_corrections(bids_root))
    for info in infos:
        if info.task is None:
            log.debug("Ignoring %s: no task name in its path", info.path)
            continue
        if info.excluded:
            log.info("Leaving out %s: marked '%s' at crosscheck", info.key, info.correction.review)
            continue
        if info.corrected:
            log.info("%s: using corrected ID %s, date %s", info.key, info.subject, info.date)
        groups.setdefault((info.subject, info.task.task), []).append(info)
    log.info("Found %d behavioural log(s) in %s", sum(map(len, groups.values())), behav_root)
    return groups


# DICOM session folder, and the scans the rules find in it: BIDS task labels, plus "T1w"
ScannedFolder = tuple[Path, frozenset[str]]
MAX_SERIES_LISTED = 12


def series_folder_names(session_folder: Path) -> list[str]:
    """The folders holding files (the series) below a session folder, by name: what to
    compare with the scan rules when none of them matched."""

    def has_files(folder: Path) -> bool:  # DICOM files often have no extension
        return any(f.is_file() for f in folder.iterdir())

    names = sorted(
        {d.name for d in session_folder.rglob("*") if d.is_dir() and has_files(d)}
        | ({session_folder.name} if has_files(session_folder) else set())
    )
    more = [f"... {len(names) - MAX_SERIES_LISTED} more"] if len(names) > MAX_SERIES_LISTED else []
    return names[:MAX_SERIES_LISTED] + more


def scan_dicom_root(
    dicom_root: Path,
    progress: Callable[[int, int], None] | None = None,
    rules: tuple[ScanRule, ...] = SCAN_RULES,
) -> list[ScannedFolder]:
    """Every dated session folder with the tasks it holds -- the slow part (walks the series
    folders), done once; corrections are applied afterwards by session_infos. progress, if
    given, is called with (folders scanned, total) as it goes."""
    scanned = []
    folders = sorted(dicom_root.iterdir())
    for n, folder in enumerate(folders, start=1):
        if progress is not None:
            progress(n, len(folders))
        if folder.name in IGNORED_FOLDERS or not folder.is_dir() or not any(folder.iterdir()):
            continue
        tasks = frozenset(rule.task_name or rule.suffix for rule in match_series(folder, rules))
        scanned.append((folder, tasks))
    return scanned


def session_infos(
    scanned: list[ScannedFolder],
    corrections: dict[str, str],
    excluded: Collection[str] = (),
    dates: dict[str, date] | None = None,
    fixed: dict[str, str] | None = None,
) -> list[SessionInfo]:
    """The sessions as the converter would number them with these ID / date / session
    corrections.

    Folders whose name has no date (and no corrected one) or whose ID is empty (and
    uncorrected) are left out, as are the excluded ones (deleted at crosscheck).
    """
    dates = dates or {}
    tasks = {folder.name: folder_tasks for folder, folder_tasks in scanned}
    found = [
        session
        for folder, _ in scanned
        if folder.name not in excluded
        and (
            session := parse_session_folder(
                folder, corrections.get(folder.name), dates.get(folder.name)
            )
        )
        is not None
    ]
    return [
        SessionInfo(
            subject_id,
            session.label,
            session.dicom.date,
            tasks[session.dicom.folder.name],
            folder=session.dicom.folder.name,
            raw_id=session.dicom.raw_id,
            time=session.dicom.time,
        )
        for subject_id, sessions in number_sessions(found, fixed).items()
        for session in sessions
    ]


def scan_sessions(
    dicom_root: Path,
    corrections: dict[str, str] | None = None,
    excluded: Collection[str] = (),
    dates: dict[str, date] | None = None,
    fixed: dict[str, str] | None = None,
    rules: tuple[ScanRule, ...] = SCAN_RULES,
) -> list[SessionInfo]:
    """Every DICOM session as the behaviour check sees it: subject, ses label, date, tasks."""
    scanned = scan_dicom_root(dicom_root, rules=rules)
    return session_infos(scanned, corrections or {}, excluded, dates, fixed)
