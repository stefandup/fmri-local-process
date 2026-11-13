"""Crosscheck of a converted Philani BIDS folder.

Works on the BIDS folder only (never the raw DICOMs) and writes nothing into it except the
human's own decisions: moving a staged file into place, or deleting one. Problems are found
from the data itself:

* missing: a NIfTI without its JSON sidecar, a task bold without its events.tsv, or a scan
  most other sessions have
* short: fewer volumes (or, for a 3D image, fewer bytes) than the same scan elsewhere --
  e.g. a series converted from an incomplete copy; an events.tsv with no events
* pending: candidates the converter staged in crosscheck_pending/
"""

import csv
import gzip
import json
import logging
import struct
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import median

from fmri_local_process.processing.behav_logs import BEHAV_TASKS
from fmri_local_process.processing.philani_bids import PENDING_DIR

log = logging.getLogger(__name__)

DATA_PATTERNS = ("*.nii.gz", "*_events.tsv")
NIFTI1_HEADER_BYTES = 348
SHORT_SIZE_FRACTION = 0.9  # a 3D image under 90% of the typical size is suspect


class CrosscheckError(Exception):
    """A crosscheck action that can't be done (e.g. its target already exists)."""


@dataclass(frozen=True)
class DataFile:
    path: Path
    subject: str
    session: str | None  # None for logs staged at subject level
    datatype: str
    label: str  # name without sub-/ses-/run- and extension, e.g. "task-Rest_bold"
    pending: bool
    size: int
    volumes: int | None = None  # NIfTI only; 1 for a 3D image
    repetition_time: float | None = None  # bold only, seconds, from the sidecar
    events: int | None = None  # events.tsv only: number of rows

    @property
    def extension(self) -> str:
        return ".nii.gz" if self.path.name.endswith(".nii.gz") else self.path.suffix

    @property
    def sidecar(self) -> Path:
        return self.path.with_name(self.path.name.removesuffix(self.extension) + ".json")

    def summary(self) -> str:
        if self.events is not None:
            return f"{self.events} events"
        if self.volumes is None:
            return "unreadable header"
        if self.volumes == 1:
            return f"3D, {self.size / 1024**2:.1f} MB"
        text = f"{self.volumes} volumes"
        if self.repetition_time:
            text += f" ({self.volumes * self.repetition_time / 60:.1f} min)"
        return text


@dataclass(frozen=True)
class Issue:
    kind: str  # "missing" / "short" / "pending"
    text: str
    path: Path | None = None


@dataclass
class SessionReview:
    subject: str
    session: str | None  # None: the subject's staged logs that belong to no session yet
    files: list[DataFile] = field(default_factory=list)
    pending: list[DataFile] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"sub-{self.subject}" + (f"_ses-{self.session}" if self.session else "")


def nifti_volumes(path: Path) -> int | None:
    """Volumes in a .nii.gz from its NIfTI-1 header (dim[4]); None if it can't be read."""
    try:
        with gzip.open(path, "rb") as f:
            header = f.read(NIFTI1_HEADER_BYTES)
        for endian in "<>":
            if struct.unpack(f"{endian}i", header[:4])[0] == NIFTI1_HEADER_BYTES:
                dims = struct.unpack(f"{endian}8h", header[40:56])
                return dims[4] if dims[0] >= 4 else 1
    except (OSError, EOFError, struct.error) as e:
        log.warning("Can't read NIfTI header of %s: %s", path.name, e)
    return None


def repetition_time(sidecar: Path) -> float | None:
    try:
        return float(json.loads(sidecar.read_text(encoding="utf-8"))["RepetitionTime"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def count_events(tsv: Path) -> int | None:
    try:
        with tsv.open(newline="", encoding="utf-8") as f:
            return sum(1 for _ in csv.DictReader(f, delimiter="\t"))
    except (OSError, csv.Error, UnicodeDecodeError):
        return None


def label_of(name: str) -> str:
    """sub-A_ses-1_task-Rest_run-2_bold.nii.gz -> task-Rest_bold"""
    stem = name.removesuffix(".nii.gz").removesuffix(".tsv")
    return "_".join(p for p in stem.split("_") if not p.startswith(("sub-", "ses-", "run-")))


def read_data_file(path: Path, subject_dir: Path, pending: bool) -> DataFile:
    parts = path.relative_to(subject_dir).parts
    session = parts[0].removeprefix("ses-") if parts[0].startswith("ses-") else None
    data = DataFile(
        path=path,
        subject=subject_dir.name.removeprefix("sub-"),
        session=session,
        datatype=path.parent.name,
        label=label_of(path.name),
        pending=pending,
        size=path.stat().st_size,
    )
    if path.name.endswith(".nii.gz"):
        volumes = nifti_volumes(path)
        tr = repetition_time(data.sidecar) if volumes and volumes > 1 else None
        return replace(data, volumes=volumes, repetition_time=tr)
    return replace(data, events=count_events(path))


def find_data_files(bids_root: Path) -> list[DataFile]:
    found = []
    for top, pending in ((bids_root, False), (bids_root / PENDING_DIR, True)):
        for subject_dir in sorted(top.glob("sub-*")):
            if not subject_dir.is_dir():
                continue
            for pattern in DATA_PATTERNS:
                found += [
                    read_data_file(p, subject_dir, pending) for p in subject_dir.rglob(pattern)
                ]
    return sorted(found, key=lambda d: d.path.as_posix())


def sidecar_note(data: DataFile) -> str:
    """The sidecar fields that tell staged candidates apart."""
    try:
        sidecar = json.loads(data.sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    keys = ("SeriesNumber", "AcquisitionTime", "SourceLog")
    return ", ".join(f"{key} {sidecar[key]}" for key in keys if key in sidecar)


def short_issue(data: DataFile, peers: list[DataFile]) -> Issue | None:
    """Compare data with the same scan elsewhere (needs at least one other copy)."""
    if data.events is not None:
        return (
            Issue("short", f"{data.path.name}: no events", data.path) if not data.events else None
        )
    if data.volumes is None:
        return Issue("short", f"{data.path.name}: NIfTI header unreadable", data.path)
    others = [p for p in peers if p.path != data.path and p.volumes is not None]
    if not others:
        return None
    if data.volumes > 1:
        typical = int(median(p.volumes for p in others if p.volumes))
        if data.volumes < typical:
            return Issue(
                "short",
                f"{data.path.name}: {data.volumes} volumes, others have {typical}",
                data.path,
            )
        return None
    typical_size = median(p.size for p in others)
    if data.size < SHORT_SIZE_FRACTION * typical_size:
        return Issue(
            "short",
            f"{data.path.name}: {data.size / 1024**2:.1f} MB, others are "
            f"{typical_size / 1024**2:.1f} MB",
            data.path,
        )
    return None


def review_bids_folder(bids_root: Path) -> list[SessionReview]:
    """Every session (plus subject-level staged logs) with the problems found in it."""
    data_files = find_data_files(bids_root)
    reviews: dict[tuple[str, str | None], SessionReview] = {}
    for session_dir in sorted(bids_root.glob("sub-*/ses-*")):  # listed even when empty
        if session_dir.is_dir():
            key = (
                session_dir.parent.name.removeprefix("sub-"),
                session_dir.name.removeprefix("ses-"),
            )
            reviews[key] = SessionReview(*key)
    for data in data_files:
        review = reviews.setdefault(
            (data.subject, data.session), SessionReview(data.subject, data.session)
        )
        (review.pending if data.pending else review.files).append(data)

    by_label: dict[str, list[DataFile]] = {}
    for data in data_files:
        by_label.setdefault(data.label, []).append(data)

    sessions = [r for r in reviews.values() if r.session is not None]
    scan_counts: dict[str, int] = {}
    for review in sessions:
        for label in {d.label for d in review.files if d.events is None}:
            scan_counts[label] = scan_counts.get(label, 0) + 1

    for review in reviews.values():
        labels = {d.label for d in review.files}
        staged_labels = {
            d.label for r in reviews.values() if r.subject == review.subject for d in r.pending
        }
        for data in review.files + review.pending:
            if data.volumes is not None and not data.sidecar.exists():
                review.issues.append(Issue("missing", f"{data.sidecar.name} (sidecar)", data.path))
            issue = short_issue(data, by_label[data.label])
            if issue:
                review.issues.append(issue)
        if review.session is None:
            review.issues += [
                Issue("pending", f"{d.path.name}: assign to a session") for d in review.pending
            ]
            continue
        for label in sorted({d.label for d in review.pending}):
            review.issues.append(Issue("pending", f"{label}: pick one of the staged runs"))
        for task in BEHAV_TASKS:
            events = f"task-{task.task}_events"
            if f"task-{task.task}_bold" in labels and events not in labels:
                where = " (staged, assign below)" if events in staged_labels else ""
                review.issues.append(Issue("missing", f"{events}.tsv{where}"))
        for label, count in sorted(scan_counts.items()):
            staged = label in {d.label for d in review.pending}
            if count * 2 > len(sessions) and label not in labels and not staged:
                review.issues.append(
                    Issue("missing", f"{label} ({count} of {len(sessions)} sessions have it)")
                )
    return sorted(reviews.values(), key=lambda r: (r.subject, r.session or ""))


def final_path(bids_root: Path, data: DataFile, session: str) -> Path:
    prefix = f"sub-{data.subject}_ses-{session}"
    return (
        bids_root
        / f"sub-{data.subject}"
        / f"ses-{session}"
        / data.datatype
        / f"{prefix}_{data.label}{data.extension}"
    )


def remove_empty_dirs(folder: Path, stop: Path) -> None:
    """Delete folder and its parents while empty, up to (not including) stop."""
    while folder != stop and folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
        folder = folder.parent


def move_into_session(bids_root: Path, data: DataFile, session: str) -> Path:
    """Move a staged file (and its sidecar) to its run-less name in the session."""
    target = final_path(bids_root, data, session)
    if target.exists():
        raise CrosscheckError(f"{target.name} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    data.path.replace(target)
    if data.sidecar.exists():
        data.sidecar.replace(target.with_name(target.name.removesuffix(data.extension) + ".json"))
    remove_empty_dirs(data.path.parent, bids_root)
    log.info("Moved %s -> %s", data.path.name, target.relative_to(bids_root))
    return target


def delete_data_file(bids_root: Path, data: DataFile) -> None:
    """Delete a file and its sidecar. The raw data still has it: re-running the conversion
    recreates a deleted scan, e.g. after the raw copy has been completed."""
    data.path.unlink()
    data.sidecar.unlink(missing_ok=True)
    if data.pending:
        remove_empty_dirs(data.path.parent, bids_root)
    log.info("Deleted %s", data.path.relative_to(bids_root))


def keep_run(bids_root: Path, chosen: DataFile, candidates: list[DataFile]) -> Path:
    """Keep chosen among one scan's staged runs; delete the others."""
    if chosen.session is None:
        raise CrosscheckError(f"{chosen.path.name} has no session")
    target = move_into_session(bids_root, chosen, chosen.session)
    for other in candidates:
        if other.path != chosen.path:
            delete_data_file(bids_root, other)
    return target


def subject_sessions(bids_root: Path, subject: str) -> list[str]:
    return sorted(
        d.name.removeprefix("ses-")
        for d in (bids_root / f"sub-{subject}").glob("ses-*")
        if d.is_dir()
    )
