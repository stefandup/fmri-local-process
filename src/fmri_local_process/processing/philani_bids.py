"""Philani DICOM -> BIDS conversion.

Python port of philani_scripts/philaniToBIDS.sh (master). Input is a folder of DICOM session
folders named ``<date>_<x>_<subject id>[...]``; only each subject's earliest session is
converted. Behavioural log conversion is NOT done here -- it moves to the processing step.
"""

import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from fmri_local_process.processing.deface import DefaceFn

log = logging.getLogger(__name__)

ConvertFn = Callable[[Path, Path, str], None]
"""(dicom series folder, output folder, file stem) -> writes <stem>.nii.gz + <stem>.json"""

DEFACE_NOTE = "The T1w image was defaced using FreeSurfer mri_deface"
SKIP_SUBJECT_ABOVE_BYTES = 1024**3  # an existing sub-* folder over 1 GB counts as done
IGNORED_FOLDERS = {"trash"}
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
    entities: str  # BIDS entities between sub-<id> and run-<n>, e.g. "task-Rest"
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
    date: str
    subject_id: str


@dataclass
class ConversionReport:
    converted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (what, why)


def bids_stem(subject_id: str, rule: ScanRule, run: int | None = None) -> str:
    parts = [f"sub-{subject_id}"]
    if rule.entities:
        parts.append(rule.entities)
    if run is not None:
        parts.append(f"run-{run}")
    parts.append(rule.suffix)
    return "_".join(parts)


def parse_session_folder(folder: Path) -> DicomSession | None:
    parts = folder.name.split("_")
    if len(parts) < 3 or not parts[0] or not parts[2]:
        return None
    return DicomSession(folder=folder, date=parts[0], subject_id=parts[2])


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
            log.warning("Skipping %s: name isn't <date>_<x>_<subject id>", folder.name)
            continue
        sessions.append(session)
    return sessions


def earliest_sessions(sessions: list[DicomSession]) -> list[DicomSession]:
    """One session per subject: the earliest date (string sort, as the bash script did)."""
    earliest: dict[str, DicomSession] = {}
    for session in sessions:
        current = earliest.get(session.subject_id)
        if current is None or session.date < current.date:
            earliest[session.subject_id] = session
    return sorted(earliest.values(), key=lambda s: s.subject_id)


def write_dataset_description(bids_root: Path) -> None:
    path = bids_root / "dataset_description.json"
    if path.exists():
        return
    path.write_text(json.dumps(DATASET_DESCRIPTION, indent=2) + "\n", encoding="utf-8")
    log.info("Created %s", path)


def reset_bids_root(bids_root: Path) -> None:
    """Remove sub-* folders and the crosscheck files; keep everything else."""
    for subject_dir in bids_root.glob("sub-*"):
        if subject_dir.is_dir():
            shutil.rmtree(subject_dir)
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


def matching_series(session_dirs: list[Path], rule: ScanRule) -> list[Path]:
    return [d for d in session_dirs if fnmatchcase(d.as_posix(), rule.pattern)]


def convert_session(
    session: DicomSession,
    bids_root: Path,
    convert: ConvertFn,
    deface: DefaceFn | None,
    report: ConversionReport,
) -> None:
    subject_dir = bids_root / f"sub-{session.subject_id}"
    if subject_dir.is_dir() and folder_size(subject_dir) > SKIP_SUBJECT_ABOVE_BYTES:
        log.info("sub-%s already converted (>1 GB), skipping", session.subject_id)
        report.skipped.append(f"sub-{session.subject_id}")
        return

    session_dirs = sorted(d for d in session.folder.rglob("*") if d.is_dir())
    for rule in SCAN_RULES:
        series = matching_series(session_dirs, rule)
        for run, series_dir in enumerate(series, start=1):
            stem = bids_stem(session.subject_id, rule, run if len(series) > 1 else None)
            try:
                convert_series(series_dir, subject_dir / rule.datatype, stem, rule, convert, deface)
                report.converted.append(stem)
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


def remove_stray_logs(bids_root: Path) -> None:
    for log_file in bids_root.rglob("*.log"):
        log_file.unlink()


def convert_dicom_root(
    dicom_root: Path,
    bids_root: Path,
    convert: ConvertFn,
    deface: DefaceFn | None,
) -> ConversionReport:
    report = ConversionReport()
    write_dataset_description(bids_root)
    sessions = find_sessions(dicom_root)
    selected = earliest_sessions(sessions)
    for skipped in sorted(set(sessions) - set(selected), key=lambda s: s.folder.name):
        log.info(
            "Skipping %s: not the earliest session for sub-%s",
            skipped.folder.name,
            skipped.subject_id,
        )
    for session in selected:
        log.info("sub-%s from %s", session.subject_id, session.folder.name)
        convert_session(session, bids_root, convert, deface, report)
    remove_stray_logs(bids_root)
    return report
