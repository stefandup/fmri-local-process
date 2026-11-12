"""Philani DICOM -> BIDS conversion.

Python port of philani_scripts/philaniToBIDS.sh (master). Input is a folder of DICOM session
folders named ``<YYYYMMDD>_<x>_<subject id>[...]``. Every session is converted, as
``sub-<id>/ses-<n>/`` with n = 1, 2, ... in calendar order of the scan date.

Anything ambiguous is staged in ``crosscheck_pending/`` (listed in .bidsignore) for the user
to resolve at crosscheck, instead of the converter guessing:

* a scan with several series in one session -> every series as ``run-<n>``
* behavioural logs, unless there is exactly one log and exactly one session with that task
"""

import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from fnmatch import fnmatchcase
from pathlib import Path

from fmri_local_process.processing.behav_logs import (
    BEHAV_TASKS,
    BehavTask,
    find_subject_logs,
    read_log_lines,
    write_events_tsv,
)
from fmri_local_process.processing.deface import DefaceFn

log = logging.getLogger(__name__)

ConvertFn = Callable[[Path, Path, str], None]
"""(dicom series folder, output folder, file stem) -> writes <stem>.nii.gz + <stem>.json"""

DEFACE_NOTE = "The T1w image was defaced using FreeSurfer mri_deface"
DATE_FORMAT = "%Y%m%d"  # date at the start of each DICOM session folder name
SKIP_SESSION_ABOVE_BYTES = 1024**3  # an existing ses-* folder over 1 GB counts as done
IGNORED_FOLDERS = {"trash"}
PENDING_DIR = "crosscheck_pending"
RESET_FILES = ("crosscheck.json", "selected_runs.json")

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
)


@dataclass(frozen=True)
class DicomSession:
    folder: Path
    date: date
    subject_id: str


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
    pending: list[str] = field(default_factory=list)  # staged in crosscheck_pending/
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


def parse_session_folder(folder: Path) -> DicomSession | None:
    """<YYYYMMDD>_<x>_<subject id>[...] -> DicomSession; None if the name doesn't fit."""
    parts = folder.name.split("_")
    if len(parts) < 3 or not parts[2]:
        return None
    try:
        scan_date = datetime.strptime(parts[0], DATE_FORMAT).date()
    except ValueError:
        return None
    return DicomSession(folder=folder, date=scan_date, subject_id=parts[2])


def find_sessions(dicom_root: Path) -> list[DicomSession]:
    sessions = []
    for folder in sorted(dicom_root.iterdir()):
        if folder.name in IGNORED_FOLDERS or not folder.is_dir():
            continue
        if not any(folder.iterdir()):
            log.warning("Skipping empty folder %s", folder.name)
            continue
        session = parse_session_folder(folder)
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


def ensure_bidsignore(bids_root: Path) -> None:
    """Keep the BIDS validator out of the crosscheck staging area."""
    path = bids_root / ".bidsignore"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    entry = f"{PENDING_DIR}/"
    if entry not in lines:
        path.write_text("\n".join([*lines, entry]) + "\n", encoding="utf-8")


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


def match_series(session_folder: Path) -> dict[ScanRule, list[Path]]:
    """Series folders per scan rule (only rules with at least one match)."""
    session_dirs = sorted(d for d in session_folder.rglob("*") if d.is_dir())
    matches = {
        rule: [d for d in session_dirs if fnmatchcase(d.as_posix(), rule.pattern)]
        for rule in SCAN_RULES
    }
    return {rule: series for rule, series in matches.items() if series}


def convert_session(
    session: BidsSession,
    series_by_rule: dict[ScanRule, list[Path]],
    bids_root: Path,
    convert: ConvertFn,
    deface: DefaceFn | None,
    report: ConversionReport,
) -> None:
    session_dir = bids_root / session.rel_dir
    if session_dir.is_dir() and folder_size(session_dir) > SKIP_SESSION_ABOVE_BYTES:
        log.info("%s already converted (>1 GB), skipping", session.prefix)
        report.skipped.append(session.prefix)
        return

    for rule, series in series_by_rule.items():
        final_dir = session_dir / rule.datatype
        final_stem = bids_stem(session.prefix, rule.entities, rule.suffix)
        if len(series) == 1:
            targets = [(series[0], final_dir, final_stem, report.converted)]
        elif (final_dir / f"{final_stem}.nii.gz").exists():
            log.info("%s: run already chosen at crosscheck, not restaging", final_stem)
            continue
        else:
            pending_dir = bids_root / PENDING_DIR / session.rel_dir / rule.datatype
            log.warning("%s: %d series, staging all for crosscheck", final_stem, len(series))
            targets = [
                (
                    s,
                    pending_dir,
                    bids_stem(session.prefix, rule.entities, rule.suffix, run),
                    report.pending,
                )
                for run, s in enumerate(series, start=1)
            ]
        for series_dir, out_dir, stem, bucket in targets:
            try:
                convert_series(series_dir, out_dir, stem, rule, convert, deface)
                bucket.append(stem)
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
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    nifti = out_dir / f"{stem}.nii.gz"
    sidecar = out_dir / f"{stem}.json"

    if nifti.exists() and sidecar.exists():
        log.info("%s exists, not reconverting", stem)
    else:
        log.info("Converting %s -> %s", series_dir.name, stem)
        convert(series_dir, out_dir, stem)
        strip_duplicate_suffix(out_dir)
        if not (nifti.exists() and sidecar.exists()):
            raise FileNotFoundError(f"dcm2niix produced no {nifti.name} / {sidecar.name}")

    if rule.suffix == "T1w" and deface is not None and not is_defaced(sidecar):
        log.info("Defacing %s", nifti.name)
        deface(nifti)
        update_json(sidecar, Notes=DEFACE_NOTE)

    if rule.task_name:
        update_json(sidecar, TaskName=rule.task_name)


def convert_behaviour(
    subject_id: str,
    sessions_with_task: list[BidsSession],
    task: BehavTask,
    behav_root: Path,
    bids_root: Path,
    report: ConversionReport,
) -> None:
    """Events for one subject/task: straight into the session if unambiguous, else staged."""
    logs = find_subject_logs(behav_root / task.log_subdir, subject_id)
    entities = f"task-{task.task}"
    if not logs:
        if sessions_with_task:
            what = f"sub-{subject_id}_{entities}_events"
            report.failures.append((what, f"no log in {behav_root / task.log_subdir}"))
        return

    if len(logs) == 1 and len(sessions_with_task) == 1:
        session = sessions_with_task[0]
        stem = bids_stem(session.prefix, entities, "events")
        targets = [
            (logs[0], bids_root / session.rel_dir / "func" / f"{stem}.tsv", report.converted)
        ]
    else:
        final = [
            bids_root / s.rel_dir / "func" / f"{bids_stem(s.prefix, entities, 'events')}.tsv"
            for s in sessions_with_task
        ]
        if any(p.exists() for p in final):
            return  # already resolved at crosscheck
        pending_dir = bids_root / PENDING_DIR / f"sub-{subject_id}" / "func"
        log.warning(
            "sub-%s %s: %d log(s) for %d session(s), staging for crosscheck",
            subject_id,
            entities,
            len(logs),
            len(sessions_with_task),
        )
        targets = [
            (
                log_file,
                pending_dir / f"{bids_stem(f'sub-{subject_id}', entities, 'events', run)}.tsv",
                report.pending,
            )
            for run, log_file in enumerate(logs, start=1)
        ]

    for log_file, tsv, bucket in targets:
        if tsv.exists():
            continue
        try:
            write_events_tsv(task.extract(read_log_lines(log_file)), tsv)
            tsv.with_suffix(".json").write_text(
                json.dumps({"SourceLog": log_file.name}, indent=2) + "\n", encoding="utf-8"
            )
            bucket.append(tsv.name.removesuffix(".tsv"))
        except (OSError, ValueError, KeyError) as e:
            log.error("%s from %s failed: %s", tsv.name, log_file.name, e)
            report.failures.append((tsv.name, f"{log_file.name}: {e!r}"))


def remove_stray_logs(bids_root: Path) -> None:
    for log_file in bids_root.rglob("*.log"):
        log_file.unlink()


def convert_dicom_root(
    dicom_root: Path,
    bids_root: Path,
    convert: ConvertFn,
    deface: DefaceFn | None,
    behav_root: Path | None = None,
) -> ConversionReport:
    report = ConversionReport()
    write_dataset_description(bids_root)
    ensure_bidsignore(bids_root)
    for subject_id, sessions in number_sessions(find_sessions(dicom_root)).items():
        series = {s: match_series(s.dicom.folder) for s in sessions}
        for session in sessions:
            log.info("%s from %s", session.prefix, session.dicom.folder.name)
            convert_session(session, series[session], bids_root, convert, deface, report)
        if behav_root is None:
            continue
        for task in BEHAV_TASKS:
            with_task = [s for s in sessions if any(r.task_name == task.task for r in series[s])]
            convert_behaviour(subject_id, with_task, task, behav_root, bids_root, report)
    remove_stray_logs(bids_root)
    return report
