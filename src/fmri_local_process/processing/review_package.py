"""Review packages: the toolbox plus a chosen part of the data, so someone else can review the
processing (`.\\build.ps1 -Review`: pick in gui/review_package_dialog.py, then this assembles).

Only what was ticked goes in:
* BIDS: the chosen subjects' chosen sessions and scans. A task's files (bold, sbref, events,
  physio) go together. The participants and sessions tables keep only the shared rows, and the
  crosscheck records in code/ only the entries about shared sessions, so nothing about the
  other participants goes along.
* C-PAC outputs of those sessions and tasks, by kind (preprocessed BOLD, QC, motion, ...).
* MRIQC's results of the shared scans: their metrics (IQMs), HTML reports and report figures.
  Never the group reports (group_*.tsv/.html): they hold every participant's runs.
* The ticked behavioural logs, at the same paths under the log folder (their corrections in
  code/behav_corrections.json are keyed by that path).
* Only when asked: the raw DICOM series the shared scans were converted from.

Layout of a package (folders as in the sources, so the app reads them as usual):
    <name>/fmri_toolbox.exe, _internal/   the app
    <name>/review_package.json            what's in it, and where (review_mode.MANIFEST)
    <name>/review_data/bids|output|mriqc|behav|dicom|atlases/
"""

import csv
import io
import json
import logging
import re
import shutil
import stat
import textwrap
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fmri_local_process.processing.atlases import atlases_present, cache_files, default_cache
from fmri_local_process.processing.behav_logs import find_logs, log_subject_id, task_for_log
from fmri_local_process.processing.behav_match import read_corrections
from fmri_local_process.processing.cpac_outputs import bids_entities
from fmri_local_process.processing.mriqc import RATINGS_FILE
from fmri_local_process.processing.review_mode import DATA_DIR, MANIFEST
from fmri_local_process.processing.session_sync import read_sessions_tsv
from fmri_local_process.processing.tab_guide import TAB_INTROS, shown_tabs

log = logging.getLogger(__name__)

# Output kinds the picker offers, in its order: key -> what it is.
OUTPUT_KINDS = {
    "preproc_bold": "Preprocessed BOLD (first-level stats need it)",
    "qc": "QC figures and values",
    "motion": "Motion and nuisance",
    "anat": "Anatomical outputs",
    "other": "Other C-PAC outputs",
    "logs": "C-PAC logs",
    "first_level": "Saved first-level maps",
}
# BIDS code/ files with nothing about individual participants: shared whole.
WHOLE_CODE_FILES = {"expected_volumes.json", "crosscheck_settings.json", "scan_rules.json"}
# code/ files of {kind: {key: value}}: filtered per inner key.
NESTED_CODE_FILES = {"crosscheck_decisions.json"}
SUBJECT_REF = re.compile(r"sub-([A-Za-z0-9]+)(?:[_/\\]ses-([A-Za-z0-9]+))?")


class ReviewPackageError(Exception):
    """The package can't be made as planned (e.g. its folder holds something else)."""


@dataclass(frozen=True, order=True)
class ScanChoice:
    subject: str
    session: str
    scan: str  # "task-Rest_bold", "T1w"


@dataclass
class Plan:
    """What goes into a package; saved as JSON between the picker and the build."""

    name: str
    bids_root: Path
    output_root: Path | None = None
    behav_root: Path | None = None
    dicom_root: Path | None = None
    mriqc_root: Path | None = None
    scans: list[ScanChoice] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)  # OUTPUT_KINDS keys
    behav_logs: list[str] = field(default_factory=list)  # paths relative to behav_root
    include_dicom: bool = False
    include_mriqc: bool = True  # MRIQC's results of the shared scans, if there are any
    include_atlases: bool = True
    view_only: bool = True  # the app hides Setup and Preprocess, locks changing actions
    make_zip: bool = True

    @property
    def subjects(self) -> list[str]:
        return sorted({c.subject for c in self.scans})

    def sessions(self) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for choice in self.scans:
            found.setdefault(choice.subject, set()).add(choice.session)
        return found

    def shares(self, subject: str, session: str | None, scan: str | None = None) -> bool:
        """Is this subject's session (and scan, when given) in the package?"""
        return any(
            c.subject == subject
            and (session is None or c.session == session)
            and (scan is None or c.scan == scan)
            for c in self.scans
        )

    def to_json(self) -> str:
        def path(p: Path | None) -> str | None:
            return str(p) if p else None

        return json.dumps(
            {
                "name": self.name,
                "bids_root": str(self.bids_root),
                "output_root": path(self.output_root),
                "behav_root": path(self.behav_root),
                "dicom_root": path(self.dicom_root),
                "mriqc_root": path(self.mriqc_root),
                "scans": [[c.subject, c.session, c.scan] for c in sorted(self.scans)],
                "outputs": self.outputs,
                "behav_logs": self.behav_logs,
                "include_dicom": self.include_dicom,
                "include_mriqc": self.include_mriqc,
                "include_atlases": self.include_atlases,
                "view_only": self.view_only,
                "make_zip": self.make_zip,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "Plan":
        raw = json.loads(text)

        def path(key: str) -> Path | None:
            return Path(raw[key]) if raw.get(key) else None

        return cls(
            name=raw["name"],
            bids_root=Path(raw["bids_root"]),
            output_root=path("output_root"),
            behav_root=path("behav_root"),
            dicom_root=path("dicom_root"),
            mriqc_root=path("mriqc_root"),
            scans=[ScanChoice(*c) for c in raw.get("scans", [])],
            outputs=list(raw.get("outputs", [])),
            behav_logs=list(raw.get("behav_logs", [])),
            include_dicom=bool(raw.get("include_dicom", False)),
            include_mriqc=bool(raw.get("include_mriqc", True)),
            include_atlases=bool(raw.get("include_atlases", True)),
            view_only=bool(raw.get("view_only", True)),
            make_zip=bool(raw.get("make_zip", True)),
        )


# ---- what there is to share ------------------------------------------------------------------


def scan_of(name: str) -> str:
    """The shareable scan a BIDS file belongs to: a task's files (bold, sbref, events, physio)
    go with 'task-<t>_bold'; anything else by its suffix ('T1w', 'epi')."""
    if task := bids_entities(name).get("task"):
        return f"task-{task}_bold"
    return name.split(".", 1)[0].rsplit("_", 1)[-1]


def bids_scans(bids_root: Path) -> list[ScanChoice]:
    """Every subject / session / scan in the BIDS folder (from its NIfTIs)."""
    found = set()
    for nifti in bids_root.glob("sub-*/ses-*/*/*.nii.gz"):
        session_dir = nifti.parent.parent
        found.add(
            ScanChoice(
                session_dir.parent.name.removeprefix("sub-"),
                session_dir.name.removeprefix("ses-"),
                scan_of(nifti.name),
            )
        )
    return sorted(found)


@dataclass(frozen=True)
class BehavLog:
    key: str  # path relative to the log folder, as code/behav_corrections.json keys it
    subject: str  # after the crosscheck's ID correction
    task: str  # BIDS task label, '' if it can't be told


def behav_logs(behav_root: Path, bids_root: Path, subjects: Iterable[str]) -> list[BehavLog]:
    """The behavioural logs of these subjects, by their (corrected) subject ID."""
    wanted = set(subjects)
    corrections = read_corrections(bids_root) if bids_root.is_dir() else {}
    logs = []
    for path in find_logs(behav_root):
        key = path.relative_to(behav_root).as_posix()
        correction = corrections.get(key)
        subject = correction.subject if correction and correction.subject else None
        subject = subject or log_subject_id(path)
        if subject in wanted:
            task = task_for_log(path, behav_root)
            logs.append(BehavLog(key, subject, task.task if task else ""))
    return logs


def default_behav_logs(logs: list[BehavLog], plan: Plan) -> list[str]:
    """The logs of tasks shared for their subject (in any session)."""
    return [b.key for b in logs if b.task and plan.shares(b.subject, None, f"task-{b.task}_bold")]


# ---- choosing the files ---------------------------------------------------------------------


def bids_files(plan: Plan) -> list[Path]:
    """BIDS files copied as they are (tables and code/ are written cut down instead)."""
    root = plan.bids_root
    files = [
        f
        for f in root.iterdir()
        if f.is_file() and f.name != "participants.tsv" and not f.name.startswith(".~")
    ]
    sessions = plan.sessions()
    for subject, shared in sessions.items():
        subject_dir = root / f"sub-{subject}"
        for path in sorted(subject_dir.rglob("*")):
            if not path.is_file() or path.name.endswith(("_sessions.tsv", "_scans.tsv")):
                continue
            parts = path.relative_to(subject_dir).parts
            if len(parts) == 1:  # beside the sessions (sessions.tsv is written)
                files.append(path)
                continue
            session = parts[0].removeprefix("ses-")
            if session in shared and plan.shares(subject, session, scan_of(path.name)):
                files.append(path)
    return files


def session_of(path: Path) -> str | None:
    """The session a C-PAC output belongs to: its ses- entity or ses-<n> folder."""
    if ses := bids_entities(path.name).get("ses"):
        return ses
    for part in path.parts:
        if part.startswith("ses-"):
            return part.removeprefix("ses-")
    found = re.search(r"ses-([A-Za-z0-9]+)", path.as_posix())
    return found.group(1) if found else None


def output_kind(path: Path) -> str:
    name = path.name
    if "desc-preproc" in name and "_bold" in name:
        return "preproc_bold"
    if "_quality." in name or "Snr" in name:
        return "qc"
    if any(word in name for word in ("motion", "regressors", "movement", "framewise", "dvars")):
        return "motion"
    if path.parent.name == "anat":
        return "anat"
    return "other"


def output_files(plan: Plan) -> list[tuple[Path, str]]:
    """(file, OUTPUT_KINDS key) of every C-PAC output of the shared sessions and tasks."""
    root = plan.output_root
    if root is None or not root.is_dir():
        return []
    found = []
    for subject, shared in plan.sessions().items():
        for path in (root / "output").glob(f"pipeline_*/sub-{subject}/**/*"):
            if not path.is_file() or session_of(path) not in shared:
                continue
            task = bids_entities(path.name).get("task")
            if task and not plan.shares(subject, session_of(path), f"task-{task}_bold"):
                continue
            found.append((path, output_kind(path)))
        for path in (root / "log").rglob("*"):
            if path.is_file() and refers_to(path.relative_to(root).as_posix(), subject, shared):
                found.append((path, "logs"))
        for path in (root / "first_level" / f"sub-{subject}").rglob("*"):
            session = session_of(path.relative_to(root))
            if path.is_file() and (session is None or session in shared):
                found.append((path, "first_level"))
    return found


def refers_to(text: str, subject: str, sessions: set[str]) -> bool:
    """Is this (a log path) about the subject, and not about a session that isn't shared?"""
    refs = [m for m in SUBJECT_REF.finditer(text) if m.group(1) == subject]
    return bool(refs) and all(m.group(2) is None or m.group(2) in sessions for m in refs)


def chosen_outputs(plan: Plan) -> list[Path]:
    return [path for path, kind in output_files(plan) if kind in plan.outputs]


# MRIQC output folder files beside the per-run results that say nothing about participants.
MRIQC_DATASET_FILES = ("dataset_description.json", ".bidsignore")


def mriqc_files(plan: Plan) -> list[Path]:
    """MRIQC's results of the shared scans: per-run IQMs and timeseries (sub-*/ses-*/), report
    figures (sub-*/figures/) and the HTML reports beside them. The group reports and MRIQC's
    own logs stay home: they mention every participant."""
    root = plan.mriqc_root
    if root is None or not root.is_dir():
        return []

    def shared(path: Path) -> bool:
        entities = bids_entities(path.name)
        subject, session = entities.get("sub"), entities.get("ses")
        if subject is None or session is None:
            return False
        return plan.shares(subject, session, scan_of(path.name))

    found = [root / name for name in MRIQC_DATASET_FILES if (root / name).is_file()]
    found += [p for p in sorted(root.glob("sub-*.html")) if shared(p)]
    for subject in plan.subjects:
        found += [
            p for p in sorted((root / f"sub-{subject}").rglob("*")) if p.is_file() and shared(p)
        ]
    return found


def has_mriqc_results(files: list[Path]) -> bool:
    """Is there more than the dataset files (a run's metrics or report)?"""
    return any(p.name not in MRIQC_DATASET_FILES for p in files)


def dicom_series_dirs(plan: Plan) -> list[Path]:
    """The raw series folders the shared scans were converted from."""
    from fmri_local_process.processing.sequence_params import dicom_series

    if plan.dicom_root is None or not plan.dicom_root.is_dir():
        return []
    folders: set[Path] = set()
    for subject in plan.subjects:
        try:
            stems = dicom_series(plan.dicom_root, plan.bids_root, subject)
        except OSError as e:
            log.warning("Can't match sub-%s's DICOM series: %s", subject, e)
            continue
        for stem, series in stems.items():
            session = bids_entities(stem).get("ses", "")
            if plan.shares(subject, session, scan_of(stem)):
                folders.update(series)
    return sorted(folders)


def size_of(paths: Iterable[Path]) -> int:
    total = 0
    for path in paths:
        try:
            if path.is_dir():
                total += sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            else:
                total += path.stat().st_size
        except OSError:
            continue
    return total


def human_size(n_bytes: int) -> str:
    size = float(n_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# ---- cutting records down to the shared sessions --------------------------------------------


@dataclass(frozen=True)
class Sharing:
    """What a record may mention: the shared sessions, their DICOM folders and the shared
    logs, and the DICOM folders of everything else (which it may not)."""

    sessions: dict[str, set[str]]
    folders: frozenset[str]
    other_folders: frozenset[str]
    logs: frozenset[str]

    @classmethod
    def of(cls, plan: Plan) -> "Sharing":
        sessions = plan.sessions()
        folders, others = set(), set()
        for subject_dir in plan.bids_root.glob("sub-*"):
            subject = subject_dir.name.removeprefix("sub-")
            for row in read_sessions_tsv(plan.bids_root, subject):
                folder = row.get("source_folder", "")
                session = row.get("session_id", "").removeprefix("ses-")
                if folder:
                    shared = session in sessions.get(subject, set())
                    (folders if shared else others).add(folder)
        return cls(
            sessions, frozenset(folders), frozenset(others - folders), frozenset(plan.behav_logs)
        )

    def allows(self, text: str) -> bool:
        """Mentions something shared, and nothing that isn't."""
        if any(folder in text for folder in self.other_folders):
            return False
        refs = list(SUBJECT_REF.finditer(text))
        for m in refs:
            shared = self.sessions.get(m.group(1))
            if shared is None or (m.group(2) is not None and m.group(2) not in shared):
                return False
        return bool(refs) or any(ref in text for ref in self.folders | self.logs)


def filter_rows(text: str, keep: Callable[[dict[str, str]], bool]) -> str:
    """A TSV with only the rows `keep` accepts (header kept)."""
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    out = io.StringIO()
    writer = csv.DictWriter(
        out, fieldnames=reader.fieldnames or [], delimiter="\t", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(row for row in reader if keep(row))
    return out.getvalue()


def shared_sessions_table(text: str, shared: set[str]) -> str:
    """A sessions.tsv with only the shared sessions."""
    return filter_rows(text, lambda row: row.get("session_id", "").removeprefix("ses-") in shared)


def shared_scans_table(text: str, plan: Plan, subject: str, session: str) -> str:
    """A session's scans.tsv with only the shared scans."""
    return filter_rows(
        text,
        lambda row: plan.shares(subject, session, scan_of(Path(row.get("filename", "")).name)),
    )


def code_file(path: Path, sharing: Sharing) -> str | None:
    """A BIDS code/ file cut down to the shared sessions (None: leave it out)."""
    text = path.read_text(encoding="utf-8")
    if path.name in WHOLE_CODE_FILES:
        return text
    if path.suffix == ".tsv":
        return filter_rows(text, lambda row: sharing.allows(" ".join(row.values())))
    if path.suffix != ".json":
        return None  # unknown: may hold anything, so it stays home
    raw = json.loads(text)
    if not isinstance(raw, dict):
        return None
    if path.name in NESTED_CODE_FILES:
        kept = {
            kind: {k: v for k, v in inner.items() if sharing.allows(f"{k} {json.dumps(v)}")}
            for kind, inner in raw.items()
            if isinstance(inner, dict)
        }
    else:
        kept = {k: v for k, v in raw.items() if sharing.allows(f"{k} {json.dumps(v)}")}
    return json.dumps(kept, indent=2) + "\n"


# ---- assembling -----------------------------------------------------------------------------


def copy_under(path: Path, source_root: Path, target_root: Path) -> None:
    target = target_root / path.relative_to(source_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, target, dirs_exist_ok=True)
    else:
        shutil.copy2(path, target)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_bids(plan: Plan, target: Path) -> None:
    root = plan.bids_root
    for path in bids_files(plan):
        copy_under(path, root, target)
    subjects = {f"sub-{s}" for s in plan.subjects}
    participants = root / "participants.tsv"
    if participants.is_file():
        text = participants.read_text(encoding="utf-8")
        write_text(
            target / participants.name,
            filter_rows(text, lambda row: row.get("participant_id", "") in subjects),
        )
    for subject, shared in plan.sessions().items():
        subject_dir = root / f"sub-{subject}"
        for table in subject_dir.rglob("*_sessions.tsv"):
            text = table.read_text(encoding="utf-8")
            write_text(target / table.relative_to(root), shared_sessions_table(text, shared))
        for table in subject_dir.rglob("*_scans.tsv"):
            session = session_of(table.relative_to(root))
            if session not in shared:
                continue
            text = table.read_text(encoding="utf-8")
            write_text(
                target / table.relative_to(root),
                shared_scans_table(text, plan, subject, session),
            )
    sharing = Sharing.of(plan)
    for path in sorted((root / "code").glob("*")):
        if not path.is_file():
            continue
        try:
            text = code_file(path, sharing)
        except (OSError, ValueError) as e:
            log.warning("Left out code/%s: %s", path.name, e)
            continue
        if text is None:
            log.info("Left out code/%s (not known to be safe to share)", path.name)
        else:
            write_text(target / "code" / path.name, text)


def write_outputs(plan: Plan, target: Path) -> None:
    assert plan.output_root is not None
    for path in chosen_outputs(plan):
        copy_under(path, plan.output_root, target)
    for subject, shared in plan.sessions().items():  # the session list C-PAC ran with
        snapshot = plan.output_root / "bids_sessions" / f"sub-{subject}_sessions.tsv"
        if snapshot.is_file():
            text = snapshot.read_text(encoding="utf-8")
            write_text(
                target / snapshot.relative_to(plan.output_root),
                shared_sessions_table(text, shared),
            )


def remove_tree(folder: Path) -> None:
    """shutil.rmtree that also deletes read-only files (C-PAC writes its logs read-only, and
    copying keeps that; Windows then refuses to delete them)."""

    def make_writable(func: Callable[[str], object], path: str, _error: BaseException) -> None:
        Path(path).chmod(stat.S_IWRITE)
        func(path)

    shutil.rmtree(folder, onexc=make_writable)


def manifest(plan: Plan, folders: dict[str, str]) -> dict:
    return {
        "name": plan.name,
        "created": datetime.now().isoformat(timespec="seconds"),
        "subjects": plan.subjects,
        "scans": [f"sub-{c.subject}/ses-{c.session}/{c.scan}" for c in sorted(plan.scans)],
        "outputs": plan.outputs,
        "behav_logs": plan.behav_logs,
        "raw_dicom": plan.include_dicom,
        "mriqc": "mriqc" in folders,
        "view_only": plan.view_only,
        "folders": folders,
    }


README_WIDTH = 90


def wrapped(text: str, indent: str = "    ") -> str:
    return textwrap.fill(text, README_WIDTH, initial_indent=indent, subsequent_indent=indent + "  ")


def readme_text(plan: Plan, folders: dict[str, str], atlases: bool) -> str:
    """README_REVIEW.txt: how to open the package, what is in it, what each tab it shows is
    for, and what to send back."""
    lines = [f"Review package: {plan.name}", f"Made {datetime.now():%Y-%m-%d %H:%M}.", ""]
    lines += [
        "HOW TO OPEN",
        wrapped(
            "Open fmri_toolbox.exe (a splash screen shows for a few seconds). It starts on the "
            "data in review_data\\. Everything you change (ratings, notes, crosscheck marks) "
            "changes this copy, never the original data."
            + (
                " This package is view only: nothing can be converted, trimmed or deleted."
                if plan.view_only
                else ""
            )
        ),
        "",
        "WHAT IS IN IT",
    ]
    by_session: dict[tuple[str, str], list[str]] = {}
    for choice in sorted(plan.scans):
        by_session.setdefault((choice.subject, choice.session), []).append(choice.scan)
    lines.append(wrapped(f"Participants: {', '.join(f'sub-{s}' for s in plan.subjects)}"))
    lines.append(wrapped("BIDS scans (raw scans in BIDS form):"))
    for (subject, session), scans in by_session.items():
        lines.append(wrapped(f"sub-{subject} ses-{session}: {', '.join(scans)}", "      "))
    if "output" in folders:
        lines.append(wrapped("C-PAC outputs (the preprocessing results):"))
        lines += [wrapped(OUTPUT_KINDS[k], "      - ") for k in plan.outputs if k in OUTPUT_KINDS]
    else:
        lines.append(wrapped("C-PAC outputs: not included"))
    included = {True: "included", False: "not included"}
    lines += [
        wrapped(f"MRIQC image quality results: {included['mriqc' in folders]}"),
        wrapped(
            f"Behaviour logs: {len(plan.behav_logs)} log(s)"
            if "behav" in folders
            else "Behaviour logs: not included"
        ),
        wrapped(f"Raw DICOMs: {included['dicom' in folders]}"),
        wrapped(
            "Atlases for the first-level stats: "
            + ("included" if atlases else "not included (Docker is needed to get them)")
        ),
        "",
        "THE TABS (in the order the app shows them)",
    ]
    tabs = shown_tabs(plan.view_only, folders)
    for number, name in enumerate(tabs, start=1):
        lines.append(wrapped(f"{number}. {name}: {TAB_INTROS[name]}"))
    lines += ["", "WHAT TO SEND BACK"]
    back = []
    if "mriqc" in folders:
        back.append(f"your QC ratings: review_data\\mriqc\\{RATINGS_FILE}")
    back.append("your crosscheck marks and notes: review_data\\bids\\code\\")
    lines += [wrapped(f"- {item}") for item in back]
    lines.append(wrapped("Zipping the whole review_data\\ folder sends them all."))
    return "\n".join(lines) + "\n"


def assemble(
    plan: Plan,
    out_root: Path,
    app_dir: Path | None = None,
    progress: Callable[[str], None] = log.info,
) -> Path:
    """Make the package folder out_root/<name> (and <name>.zip if planned); returns it.

    A folder of that name is replaced only if it is an earlier package (has a MANIFEST).
    """
    if not plan.scans:
        raise ReviewPackageError("Nothing chosen to share.")
    target = out_root / plan.name
    if target.exists():
        if not (target / MANIFEST).is_file():
            raise ReviewPackageError(f"{target} exists and isn't a review package: not replaced.")
        remove_tree(target)
    if app_dir is not None:
        progress(f"Copying the app from {app_dir}")
        shutil.copytree(app_dir, target)
    target.mkdir(parents=True, exist_ok=True)
    data = target / DATA_DIR
    folders = {}

    progress("Copying BIDS")
    write_bids(plan, data / "bids")
    folders["bids"] = f"{DATA_DIR}/bids"
    if plan.output_root is not None and plan.outputs:
        progress("Copying C-PAC outputs")
        write_outputs(plan, data / "output")
        folders["output"] = f"{DATA_DIR}/output"
    mriqc = mriqc_files(plan) if plan.include_mriqc else []
    if plan.mriqc_root is not None and has_mriqc_results(mriqc):
        progress("Copying MRIQC results")
        for path in mriqc:
            copy_under(path, plan.mriqc_root, data / "mriqc")
        folders["mriqc"] = f"{DATA_DIR}/mriqc"
    if plan.behav_root is not None and plan.behav_logs:
        progress("Copying behavioural logs")
        for key in plan.behav_logs:
            copy_under(plan.behav_root / key, plan.behav_root, data / "behav")
        folders["behav"] = f"{DATA_DIR}/behav"
    if plan.include_dicom and plan.dicom_root is not None:
        progress("Copying raw DICOM series")
        for series in dicom_series_dirs(plan):
            copy_under(series, plan.dicom_root, data / "dicom")
        folders["dicom"] = f"{DATA_DIR}/dicom"
    cache = default_cache()
    if plan.include_atlases and atlases_present(cache):
        progress("Copying the atlases")
        for name in cache_files():
            copy_under(cache / name, cache, data / "atlases")

    write_text(target / MANIFEST, json.dumps(manifest(plan, folders), indent=2) + "\n")
    write_text(
        target / "README_REVIEW.txt", readme_text(plan, folders, (data / "atlases").is_dir())
    )
    if plan.make_zip:
        progress(f"Zipping {target.name}.zip")
        zip_path = target.with_suffix(".zip")
        zip_path.unlink(missing_ok=True)
        shutil.make_archive(str(target), "zip", root_dir=target.parent, base_dir=target.name)
    progress(f"Review package ready: {target}")
    return target
