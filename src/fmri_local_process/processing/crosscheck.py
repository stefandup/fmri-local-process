"""Crosscheck of a converted Philani BIDS folder.

Works on the BIDS folder only (never the raw DICOMs) and writes nothing into it except the
human's own decisions: keeping one of several runs, trimming, or deleting a file. Problems are
found from the data itself:

* missing: a NIfTI without its JSON sidecar, a task bold without its events.tsv, or a scan
  most other sessions have
* short: fewer volumes (or, for a 3D image, fewer bytes) than the same scan elsewhere, or
  than its expected length -- e.g. a series converted from an incomplete copy; an
  events.tsv with no events
* long: more volumes than its expected length (the scan was stopped by hand after the
  task); trim_run keeps the first volumes
* pending: a scan converted as several runs (run-1, run-2, ...): keep one
* check: an events.tsv made from a log dated other than its scan
* extra: a subject with more sessions than the study expects (set per BIDS folder; default
  1): one may be a dud visit to delete

A scan's expected length is the number the human set for it (e.g. task-Matching_bold: 441),
else the (upper) median volume count of its runs, else (a scan with a single run) the
study's usual length. Set numbers are kept in the BIDS folder's code/expected_volumes.json,
so they travel with the data. Session notes (code/session_notes.json) and the number of
sessions expected (code/crosscheck_settings.json) live there too, and every change made
here is logged in code/crosscheck_actions.tsv.
"""

import csv
import gzip
import json
import logging
import shutil
import struct
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import median, median_high

import nibabel as nib
import numpy as np

from fmri_local_process.processing.action_log import record_action
from fmri_local_process.processing.behav_logs import BEHAV_TASKS
from fmri_local_process.processing.philani_bids import exclude_session
from fmri_local_process.processing.session_sync import read_sessions_tsv

log = logging.getLogger(__name__)

DATA_PATTERNS = ("*.nii.gz", "*_events.tsv")
NIFTI1_HEADER_BYTES = 348
SHORT_SIZE_FRACTION = 0.9  # a 3D image under 90% of the typical size is suspect
EXPECTED_FILE = Path("code") / "expected_volumes.json"
SETTINGS_FILE = Path("code") / "crosscheck_settings.json"
NOTES_FILE = Path("code") / "session_notes.json"
DEFAULT_EXPECTED_SESSIONS = 1
# The Philani protocol's usual BOLD lengths (volumes), from the philani_scripts MRI toolbox.
# Used only for a scan with a single run, which has no median to compare with.
STUDY_VOLUMES = {
    "task-Matching_bold": 495,
    "task-Reappraisal_bold": 500,
    "task-Rest_bold": 495,
    "task-Peer_bold": 140,
}
CSV_COLUMNS = ("subject", "session", "date", "source_folder", "files", "problem", "detail", "note")


class CrosscheckError(Exception):
    """A crosscheck action that can't be done (e.g. its target already exists)."""


@dataclass(frozen=True)
class DataFile:
    path: Path
    subject: str
    session: str | None  # None only for a file outside ses-* (not made by the converter)
    datatype: str
    label: str  # name without sub-/ses-/run- and extension, e.g. "task-Rest_bold"
    pending: bool  # one of several runs of a scan (run-<n> in its name): keep one
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
    kind: str  # "missing" / "short" / "long" / "pending" / "check" / "extra"
    text: str
    path: Path | None = None
    trim_to: int | None = None  # "long" only: the expected volumes


@dataclass
class SessionReview:
    subject: str
    session: str | None
    files: list[DataFile] = field(default_factory=list)
    pending: list[DataFile] = field(default_factory=list)  # runs to choose from
    issues: list[Issue] = field(default_factory=list)
    acq_time: str = ""  # scan date/time from sub-<id>_sessions.tsv, e.g. 2024-10-14T13:41:00
    source_folder: str = ""  # DICOM session folder it was converted from
    note: str = ""  # the human's note (code/session_notes.json)

    @property
    def note_key(self) -> str:
        """Notes follow the DICOM folder, so they survive session renumbering."""
        return self.source_folder or self.name

    @property
    def date(self) -> str:
        """'2024-10-14 13:41' (or just the date); '' if unknown."""
        return self.acq_time.replace("T", " ")[:16]

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


def bold_run_length(
    bids_root: Path, subject: str, session: str, task: str
) -> tuple[int, float | None] | None:
    """(volumes, TR) of a session's converted task bold, or None if it isn't in BIDS yet."""
    stem = f"sub-{subject}_ses-{session}_task-{task}_bold"
    nifti = bids_root / f"sub-{subject}" / f"ses-{session}" / "func" / f"{stem}.nii.gz"
    if not nifti.is_file():
        return None
    volumes = nifti_volumes(nifti)
    if volumes is None:
        return None
    return volumes, repetition_time(nifti.with_name(f"{stem}.json"))


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


def read_data_file(path: Path, subject_dir: Path) -> DataFile:
    parts = path.relative_to(subject_dir).parts
    session = parts[0].removeprefix("ses-") if parts[0].startswith("ses-") else None
    data = DataFile(
        path=path,
        subject=subject_dir.name.removeprefix("sub-"),
        session=session,
        datatype=path.parent.name,
        label=label_of(path.name),
        pending="_run-" in path.name,
        size=path.stat().st_size,
    )
    if path.name.endswith(".nii.gz"):
        volumes = nifti_volumes(path)
        tr = repetition_time(data.sidecar) if volumes and volumes > 1 else None
        return replace(data, volumes=volumes, repetition_time=tr)
    return replace(data, events=count_events(path))


def find_data_files(bids_root: Path) -> list[DataFile]:
    found = []
    for subject_dir in sorted(bids_root.glob("sub-*")):
        if not subject_dir.is_dir():
            continue
        for pattern in DATA_PATTERNS:
            found += [read_data_file(p, subject_dir) for p in subject_dir.rglob(pattern)]
    return sorted(found, key=lambda d: d.path.as_posix())


def sidecar_note(data: DataFile) -> str:
    """The sidecar fields that tell runs (or an events file's log) apart."""
    try:
        sidecar = json.loads(data.sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    keys = ("SeriesNumber", "AcquisitionTime", "SourceLog")
    return ", ".join(f"{key} {sidecar[key]}" for key in keys if key in sidecar)


def read_expected(bids_root: Path) -> dict[str, int]:
    """Expected volumes per scan label, as set by the human ({} when none are set)."""
    try:
        saved = json.loads((bids_root / EXPECTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): int(v) for k, v in saved.items() if isinstance(v, int) and v > 0}


def write_expected(bids_root: Path, expected: dict[str, int]) -> None:
    path = bids_root / EXPECTED_FILE
    old = read_expected(bids_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {label: n for label, n in sorted(expected.items()) if n > 0}
    path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    for label in sorted(old.keys() | kept.keys()):
        if old.get(label) != kept.get(label):
            before, after = old.get(label, "automatic"), kept.get(label, "automatic")
            record_action(bids_root, "set expected volumes", label, f"{before} -> {after}")


def read_json_dict(path: Path) -> dict:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def write_json_dict(path: Path, values: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(values.items())), indent=2) + "\n", encoding="utf-8")


def read_expected_sessions(bids_root: Path) -> int:
    """How many sessions each subject should have (more are flagged); default 1."""
    value = read_json_dict(bids_root / SETTINGS_FILE).get("expected_sessions")
    return value if isinstance(value, int) and value > 0 else DEFAULT_EXPECTED_SESSIONS


def write_expected_sessions(bids_root: Path, sessions: int) -> None:
    old = read_expected_sessions(bids_root)
    path = bids_root / SETTINGS_FILE
    write_json_dict(path, read_json_dict(path) | {"expected_sessions": sessions})
    if old != sessions:
        record_action(bids_root, "set expected sessions", "per subject", f"{old} -> {sessions}")


def read_notes(bids_root: Path) -> dict[str, str]:
    """Session notes: {DICOM source folder (or sub-X_ses-N): note}."""
    return {str(k): str(v) for k, v in read_json_dict(bids_root / NOTES_FILE).items() if v}


def set_note(bids_root: Path, reviews: list["SessionReview"], note: str) -> None:
    """Give these sessions the same note ('' clears it)."""
    notes = read_notes(bids_root)
    note = note.strip()
    for review in reviews:
        if notes.get(review.note_key, "") == note:
            continue
        if note:
            notes[review.note_key] = note
        else:
            notes.pop(review.note_key, None)
        record_action(bids_root, "note", review.name, note or "(cleared)")
    write_json_dict(bids_root / NOTES_FILE, notes)


def median_volumes(data_files: list[DataFile]) -> dict[str, int]:
    """Per 4D scan label, the upper median volume count of its runs in the main tree.

    The upper median is a length some run actually has, and with two runs (one cut short)
    it's the longer one, so a full-length run is never offered a trim down to a short one.
    """
    runs: dict[str, list[int]] = {}
    for data in data_files:
        if not data.pending and data.volumes is not None and data.volumes > 1:
            runs.setdefault(data.label, []).append(data.volumes)
    return {label: median_high(counts) for label, counts in runs.items() if len(counts) > 1}


def automatic_lengths(data_files: list[DataFile]) -> dict[str, tuple[int, str]]:
    """Expected volumes per label when the human set none: the median of its runs, else
    (a single run) the study's usual length. Values are (volumes, where it came from)."""
    lengths = {label: (n, "study default") for label, n in STUDY_VOLUMES.items()}
    lengths |= {label: (n, "median of runs") for label, n in median_volumes(data_files).items()}
    return lengths


def expected_lengths(bids_root: Path, data_files: list[DataFile]) -> dict[str, tuple[int, str]]:
    """Expected volumes per label: the human's saved number, else automatic_lengths.

    Values are (volumes, where it came from): "expected" (saved), "median of runs" or
    "study default".
    """
    lengths = automatic_lengths(data_files)
    lengths |= {label: (n, "expected") for label, n in read_expected(bids_root).items()}
    return lengths


def length_issue(data: DataFile, expected: int, source: str = "expected") -> Issue | None:
    """Compare a 4D scan with its expected length (saved, or the median of its runs)."""
    assert data.volumes is not None
    if data.volumes < expected:
        text = f"{data.path.name}: {data.volumes} volumes, {source} {expected}"
        return Issue("short", text, data.path)
    if data.volumes > expected:
        text = f"{data.path.name}: {data.volumes} volumes, {source} {expected} (trim the rest)"
        return Issue("long", text, data.path, trim_to=expected)
    return None


def short_issue(
    data: DataFile,
    peers: list[DataFile],
    expected: int | None = None,
    source: str = "expected",
) -> Issue | None:
    """Compare data with its expected length if known, else with the same scan elsewhere
    (needs at least one other copy)."""
    if data.events is not None:
        return (
            Issue("short", f"{data.path.name}: no events", data.path) if not data.events else None
        )
    if data.volumes is None:
        return Issue("short", f"{data.path.name}: NIfTI header unreadable", data.path)
    if expected and data.volumes > 1:
        return length_issue(data, expected, source)
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


def log_date_issue(data: DataFile, review: SessionReview) -> Issue | None:
    """An events.tsv from a log dated other than its session's scan (it was the only log and
    the only session, so it went there anyway): worth a look."""
    try:
        sidecar = json.loads(data.sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    log_date, scan_date = str(sidecar.get("SourceLogDate", "")), review.acq_time[:10]
    if not log_date or not scan_date or log_date == scan_date:
        return None
    return Issue(
        "check",
        f"{data.path.name}: from a log dated {log_date}, scanned {scan_date} "
        f"({sidecar.get('SourceLog', '?')})",
        data.path,
    )


def review_bids_folder(bids_root: Path) -> list[SessionReview]:
    """Every session with its scan date (sessions.tsv) and the problems found in it."""
    data_files = find_data_files(bids_root)
    expected = expected_lengths(bids_root, data_files)
    reviews: dict[tuple[str, str | None], SessionReview] = {}
    for session_dir in sorted(bids_root.glob("sub-*/ses-*")):  # listed even when empty
        if session_dir.is_dir():
            key = (
                session_dir.parent.name.removeprefix("sub-"),
                session_dir.name.removeprefix("ses-"),
            )
            reviews[key] = SessionReview(*key)
    for subject in {subject for subject, _ in reviews}:
        for row in read_sessions_tsv(bids_root, subject):
            review = reviews.get((subject, row.get("session_id", "").removeprefix("ses-")))
            if review is not None:
                review.acq_time = row.get("acq_time", "")
                review.source_folder = row.get("source_folder", "")
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
        for label in {d.label for d in review.files + review.pending if d.events is None}:
            scan_counts[label] = scan_counts.get(label, 0) + 1

    for review in reviews.values():
        labels = {d.label for d in review.files + review.pending}
        for data in review.files + review.pending:
            if data.volumes is not None and not data.sidecar.exists():
                review.issues.append(Issue("missing", f"{data.sidecar.name} (sidecar)", data.path))
            length, source = expected.get(data.label, (None, ""))
            issue = short_issue(data, by_label[data.label], length, source)
            if issue:
                review.issues.append(issue)
            if data.events is not None and (issue := log_date_issue(data, review)):
                review.issues.append(issue)
        for label in sorted({d.label for d in review.pending}):
            runs = sum(d.label == label for d in review.pending)
            review.issues.append(Issue("pending", f"{label}: {runs} runs, keep one"))
        for task in BEHAV_TASKS:
            events = f"task-{task.task}_events"
            if f"task-{task.task}_bold" in labels and events not in labels:
                review.issues.append(
                    Issue(
                        "missing",
                        f"{events}.tsv: no log placed here (see IDs & behaviour logs)",
                    )
                )
        for label, count in sorted(scan_counts.items()):
            if count * 2 > len(sessions) and label not in labels:
                review.issues.append(
                    Issue("missing", f"{label} ({count} of {len(sessions)} sessions have it)")
                )
    flag_extra_sessions(sessions, read_expected_sessions(bids_root))
    notes = read_notes(bids_root)
    for review in reviews.values():
        review.note = notes.get(review.note_key, "")
    return sorted(reviews.values(), key=lambda r: (r.subject, r.session or ""))


def flag_extra_sessions(sessions: list[SessionReview], expected: int) -> None:
    """A subject with more sessions than expected: flag each of its sessions, listing them
    all with their dates, so a dud visit (or one visit split over two days) stands out."""
    by_subject: dict[str, list[SessionReview]] = {}
    for review in sessions:
        by_subject.setdefault(review.subject, []).append(review)
    for subject, group in by_subject.items():
        if len(group) <= expected:
            continue
        group.sort(key=lambda r: int(r.session) if r.session and r.session.isdigit() else 0)
        listing = ", ".join(f"ses-{r.session} {r.date or 'date ?'}" for r in group)
        text = f"sub-{subject} has {len(group)} sessions, {expected} expected ({listing})"
        for review in group:
            review.issues.append(Issue("extra", text))


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
    """Move a file (and its sidecar), e.g. the kept run, to its run-less name in the session."""
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


def relative(bids_root: Path, path: Path) -> str:
    return path.relative_to(bids_root).as_posix()


def delete_data_file(bids_root: Path, data: DataFile, why: str = "") -> None:
    """Delete a file and its sidecar. The raw data still has it: re-running the conversion
    recreates a deleted scan, e.g. after the raw copy has been completed."""
    target = relative(bids_root, data.path)
    data.path.unlink()
    data.sidecar.unlink(missing_ok=True)
    if data.pending:
        remove_empty_dirs(data.path.parent, bids_root)
    record_action(bids_root, "delete file", target, why or data.summary())
    log.info("Deleted %s", target)


def keep_run(bids_root: Path, chosen: DataFile, candidates: list[DataFile]) -> Path:
    """Keep chosen among one scan's runs in a session (run-less name); delete the others."""
    if chosen.session is None:
        raise CrosscheckError(f"{chosen.path.name} has no session")
    source = relative(bids_root, chosen.path)
    target = move_into_session(bids_root, chosen, chosen.session)
    others = [d for d in candidates if d.path != chosen.path]
    record_action(
        bids_root,
        "keep run",
        source,
        f"{chosen.summary()}; now {target.name}; {len(others)} other run(s) deleted",
    )
    for other in others:
        delete_data_file(bids_root, other, f"other run of {chosen.path.name} ({other.summary()})")
    return target


def delete_session(bids_root: Path, review: SessionReview, why: str = "") -> None:
    """Delete a whole session folder (a dud visit) and leave its DICOM folder out of every
    conversion from now on, so it isn't recreated. The next conversion renumbers the
    subject's remaining sessions."""
    if review.session is None:
        raise CrosscheckError(f"{review.name} is not a session folder")
    if not review.source_folder:
        raise CrosscheckError(
            f"{review.name}: its sessions.tsv doesn't say which DICOM folder it came from, so "
            "a conversion would bring it back. Convert / update BIDS first, then delete it."
        )
    session_dir = bids_root / f"sub-{review.subject}" / f"ses-{review.session}"
    exclude_session(
        bids_root, review.source_folder, why or f"deleted at crosscheck ({review.name})"
    )
    if session_dir.is_dir():
        shutil.rmtree(session_dir)
    detail = f"from {review.source_folder}, scanned {review.date or '?'}"
    record_action(bids_root, "delete session", review.name, detail + (f"; {why}" if why else ""))
    log.info("Deleted %s (%s); left out of conversion from now on", review.name, detail)


def write_review_csv(reviews: list[SessionReview], path: Path) -> None:
    """Every session with each of its problems (one row each; 'ok' if none) and its note."""
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for review in reviews:
            start = [
                review.subject,
                f"ses-{review.session}" if review.session else "",
                review.date,
                review.source_folder,
                len(review.files) + len(review.pending),
            ]
            problems = [(i.kind, i.text) for i in review.issues] or [("ok", "")]
            for kind, text in problems:
                writer.writerow([*start, kind, text, review.note])


def events_end_s(data: DataFile) -> float | None:
    """When the last event of a bold run's events.tsv ends (seconds), if there is one."""
    tsv = data.path.with_name(data.path.name.removesuffix("_bold.nii.gz") + "_events.tsv")
    try:
        with tsv.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        return max((float(r["onset"]) + float(r.get("duration") or 0) for r in rows), default=None)
    except (OSError, KeyError, ValueError, csv.Error, UnicodeDecodeError):
        return None


def trim_run(bids_root: Path, data: DataFile, volumes: int) -> None:
    """Keep only the first `volumes` volumes of a 4D run (the scan ran on after the task).

    Refuses if its events.tsv has events after the cut. The sidecar records the original
    length as TrimmedFromVolumes. The raw DICOMs are untouched: deleting the file and
    converting again restores the full run.
    """
    image = nib.load(data.path)
    if not isinstance(image, nib.Nifti1Image) or len(image.shape) != 4:
        raise CrosscheckError(f"{data.path.name} is not a 4D NIfTI")
    original = image.shape[3]
    if original <= volumes:
        raise CrosscheckError(f"{data.path.name} has {original} volumes; nothing to trim")
    if data.repetition_time:
        end = events_end_s(data)
        if end is not None and end > volumes * data.repetition_time:
            raise CrosscheckError(
                f"{data.path.name}: its events run to {end:.0f} s, past the trimmed length "
                f"({volumes * data.repetition_time:.0f} s). Check the expected length."
            )
    header = image.header.copy()
    trimmed = nib.Nifti1Image(np.asarray(image.dataobj[..., :volumes]), image.affine, header)
    trimmed.set_data_dtype(image.get_data_dtype())
    partial_path = data.path.with_name(data.path.name.removesuffix(".nii.gz") + "_trim.nii.gz")
    nib.save(trimmed, partial_path)
    del image  # release the file before replacing it (Windows)
    partial_path.replace(data.path)
    try:
        sidecar = json.loads(data.sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sidecar = {}
    sidecar.setdefault("TrimmedFromVolumes", original)
    data.sidecar.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    record_action(
        bids_root, "trim", relative(bids_root, data.path), f"{original} -> {volumes} volumes"
    )
    log.info("Trimmed %s: %d -> %d volumes", data.path.name, original, volumes)
