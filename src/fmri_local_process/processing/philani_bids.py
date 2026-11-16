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
"""

import csv
import json
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from fnmatch import fnmatchcase
from pathlib import Path

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


SCAN_RULES = (
    ScanRule("*_ABCD_T1w_*_RMS", "anat", "", "T1w"),
    ScanRule("*_Cog_reappraisal_task_AP", "func", "task-Reappraisal", "bold"),
    ScanRule("*_labeling_task_AP", "func", "task-Matching", "bold"),
    ScanRule("*_rest_AP", "func", "task-Rest", "bold"),
    ScanRule("*_AP_Peer", "func", "task-Peer", "bold"),
    ScanRule("*_Checkerboard_task_AP", "func", "task-Checkerboard", "bold"),
)


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
    failures: list[tuple[str, str]] = field(default_factory=list)  # (what, why)


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
    path = bids_root / DICOM_CORRECTIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {k: v.strip() for k, v in sorted(corrections.items()) if v.strip()}
    path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")


def parse_session_folder(folder: Path, corrected_id: str | None = None) -> DicomSession | None:
    """<YYYYMMDD>_<HHMM or x>_<subject id>[...] -> DicomSession; None if it doesn't fit.

    The ID is made canonical (twin 12345-1 -> 12345T1, other non-alphanumerics dropped); a
    corrected_id recorded at crosscheck replaces the folder's own.
    """
    parts = folder.name.split("_")
    if len(parts) < 3:
        return None
    try:
        scan_date = datetime.strptime(parts[0], DATE_FORMAT).date()
    except ValueError:
        return None
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
    dicom_root: Path, corrections: dict[str, str] | None = None
) -> list[DicomSession]:
    """Every session folder in dicom_root, with crosscheck ID corrections applied."""
    sessions = []
    for folder in sorted(dicom_root.iterdir()):
        if folder.name in IGNORED_FOLDERS or not folder.is_dir():
            continue
        if not any(folder.iterdir()):
            log.warning("Skipping empty folder %s", folder.name)
            continue
        session = parse_session_folder(folder, (corrections or {}).get(folder.name))
        if session is None:
            log.warning("Skipping %s: name isn't <YYYYMMDD>_<x>_<subject id>", folder.name)
            continue
        sessions.append(session)
    return sessions


def number_sessions(sessions: list[DicomSession]) -> dict[str, list[BidsSession]]:
    """Per subject, label sessions 1, 2, ... in calendar order (folder name breaks ties).

    Labels follow the sessions present now: a newly added earlier session renumbers the rest.
    """
    by_subject: dict[str, list[DicomSession]] = {}
    for session in sessions:
        by_subject.setdefault(session.subject_id, []).append(session)
    return {
        subject_id: [
            BidsSession(dicom, str(n))
            for n, dicom in enumerate(sorted(group, key=lambda s: (s.date, s.folder.name)), 1)
        ]
        for subject_id, group in sorted(by_subject.items())
    }


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
        log.warning("sub-%s: ses-%s renumbered to ses-%s (an earlier session was added)",
                    subject_id, old, new)  # fmt: skip
        report.renumbered.append(f"sub-{subject_id}: ses-{old} -> ses-{new}")


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


def match_series(session_folder: Path) -> dict[ScanRule, list[Path]]:
    """Series folders per scan rule (only rules with at least one match)."""
    session_dirs = sorted(d for d in session_folder.rglob("*") if d.is_dir())
    matches = {
        rule: drop_copies([d for d in session_dirs if fnmatchcase(d.as_posix(), rule.pattern)])
        for rule in SCAN_RULES
    }
    return {rule: series for rule, series in matches.items() if series}


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
                problems = convert_series(series_dir, out_dir, stem, rule, convert, deface)
                bucket.append(stem)
                report.flagged.extend((stem, problem) for problem in problems)
            except (subprocess.CalledProcessError, OSError, ValueError) as e:
                log.error("%s failed: %s", stem, e)
                report.failures.append((stem, str(e)))


def convert_series(
    series_dir: Path,
    out_dir: Path,
    stem: str,
    rule: ScanRule,
    convert: ConvertFn,
    deface: DefaceFn | None,
) -> list[str]:
    """Convert one series; returns problems for the report (e.g. truncated files left out)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    nifti = out_dir / f"{stem}.nii.gz"
    sidecar = out_dir / f"{stem}.json"

    problems = []
    if nifti.exists() and sidecar.exists():
        log.info("%s exists, not reconverting", stem)
    else:
        log.info("Converting %s -> %s", series_dir.name, stem)
        problems = convert_intact(series_dir, out_dir, stem, convert)
        strip_duplicate_suffix(out_dir)
        if not (nifti.exists() and sidecar.exists()):
            raise FileNotFoundError(f"dcm2niix produced no {nifti.name} / {sidecar.name}")

    if rule.suffix == "T1w" and deface is not None and not is_defaced(sidecar):
        log.info("Defacing %s", nifti.name)
        tool = deface(nifti)
        update_json(sidecar, Notes=f"{DEFACE_NOTE} {tool}")

    if rule.task_name:
        update_json(sidecar, TaskName=rule.task_name)
    return problems


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
    log_groups = find_behav_logs(behav_root, bids_root) if behav_root is not None else {}
    found = (
        [single_session(dicom_root, subject_id)]
        if subject_id is not None
        else find_sessions(dicom_root, read_dicom_corrections(bids_root))
    )
    subjects = number_sessions(found)
    total = len(found) + (len(subjects) + 1 if behav_root is not None else 0)
    done = 0

    def step(what: str) -> None:
        nonlocal done
        if progress is not None:
            progress(done, total, what)
        done += 1

    for subject_id, sessions in subjects.items():
        renumber_sessions(bids_root, subject_id, sessions, report)
        series = {s: match_series(s.dicom.folder) for s in sessions}
        for session in sessions:
            step(f"{session.prefix} from {session.dicom.folder.name}")
            log.info("%s from %s", session.prefix, session.dicom.folder.name)
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


ScannedFolder = tuple[Path, frozenset[str]]  # DICOM session folder, BIDS tasks scanned in it


def scan_dicom_root(dicom_root: Path) -> list[ScannedFolder]:
    """Every dated session folder with the tasks it holds -- the slow part (walks the series
    folders), done once; corrections are applied afterwards by session_infos."""
    scanned = []
    for folder in sorted(dicom_root.iterdir()):
        if folder.name in IGNORED_FOLDERS or not folder.is_dir() or not any(folder.iterdir()):
            continue
        tasks = frozenset(rule.task_name for rule in match_series(folder) if rule.task_name)
        scanned.append((folder, tasks))
    return scanned


def session_infos(scanned: list[ScannedFolder], corrections: dict[str, str]) -> list[SessionInfo]:
    """The sessions as the converter would number them with these ID corrections.

    Folders whose name has no date or whose ID is empty (and uncorrected) are left out.
    """
    tasks = {folder.name: folder_tasks for folder, folder_tasks in scanned}
    found = [
        session
        for folder, _ in scanned
        if (session := parse_session_folder(folder, corrections.get(folder.name))) is not None
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
        for subject_id, sessions in number_sessions(found).items()
        for session in sessions
    ]


def scan_sessions(dicom_root: Path, corrections: dict[str, str] | None = None) -> list[SessionInfo]:
    """Every DICOM session as the behaviour check sees it: subject, ses label, date, tasks."""
    return session_infos(scan_dicom_root(dicom_root), corrections or {})
