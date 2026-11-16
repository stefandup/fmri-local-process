"""Keep C-PAC output in step with the BIDS session numbers.

The BIDS converter renumbers a subject's sessions when an earlier scan turns up later
(ses-1 -> ses-2, ...), and C-PAC names everything by session label. So when a subject is
preprocessed, a copy of its BIDS sub-<id>_sessions.tsv is kept with the output
(OUTPUT/bids_sessions/). If the BIDS one has changed since -- the same DICOM folder now has
another ses label -- the subject's output is out of step:

* re-running it clears the old results first (cpac_docker.preprocess_subject), or
* rename_outputs renames the existing output to the new numbers. C-PAC still recomputes
  every session of the subject on its next run (it runs per subject, and the BIDS paths
  changed), so renaming only keeps the existing results correctly labelled meanwhile.

The DICOM source folder is the link: it never changes, whatever the session is called.
"""

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SNAPSHOT_DIR = "bids_sessions"  # under the C-PAC output folder
RENAMING = "renumbering"  # temporary label suffix, so ses-1 <-> ses-2 can swap


@dataclass(frozen=True)
class SessionChange:
    old: str  # session label the output was made with
    new: str  # its label in BIDS now
    folder: str  # DICOM folder both labels came from

    def __str__(self) -> str:
        return f"ses-{self.old} -> ses-{self.new} ({self.folder})"


def snapshot_path(output_root: Path, subject: str) -> Path:
    return output_root / SNAPSHOT_DIR / f"sub-{subject}_sessions.tsv"


def _labels(rows: list[dict[str, str]]) -> dict[str, str]:
    """{DICOM source folder: session label without 'ses-'}."""
    return {
        row["source_folder"]: row.get("session_id", "").removeprefix("ses-")
        for row in rows
        if row.get("source_folder")
    }


def _read(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter="\t"))
    except (OSError, csv.Error):
        return []


def sessions_tsv(bids_root: Path, subject_id: str) -> Path:
    """BIDS sub-<id>/sub-<id>_sessions.tsv (written by the converter)."""
    return bids_root / f"sub-{subject_id}" / f"sub-{subject_id}_sessions.tsv"


def read_sessions_tsv(bids_root: Path, subject_id: str) -> list[dict[str, str]]:
    """Rows of sub-<id>_sessions.tsv: session_id, acq_time, source_folder ([] if none)."""
    return _read(sessions_tsv(bids_root, subject_id))


def record_sessions(bids_root: Path, output_root: Path, subject: str) -> None:
    """Keep a copy of the subject's BIDS sessions.tsv with its output (if BIDS has one)."""
    source = sessions_tsv(bids_root, subject)
    if not source.is_file():
        return
    target = snapshot_path(output_root, subject)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def adopt_sessions(bids_root: Path, output_root: Path, subject: str) -> None:
    """Output made before sessions were recorded: take the current BIDS numbering as the one
    it was made with (the same assumption the converter makes for older BIDS folders)."""
    has_output = any((output_root / "output").glob(f"pipeline_*/sub-{subject}"))
    if has_output and not snapshot_path(output_root, subject).exists():
        record_sessions(bids_root, output_root, subject)


def session_changes(bids_root: Path, output_root: Path, subject: str) -> list[SessionChange]:
    """Sessions whose label changed in BIDS since the subject's output was made ([] if none,
    or if either side has no record)."""
    then = _labels(_read(snapshot_path(output_root, subject)))
    now = _labels(read_sessions_tsv(bids_root, subject))
    return [
        SessionChange(then[folder], now[folder], folder)
        for folder in sorted(then.keys() & now.keys(), key=lambda f: then[f])
        if then[folder] != now[folder]
    ]


def _session_token(subject: str) -> re.Pattern[str]:
    """'[sub-]<subject>_ses-<label>' as a whole token (not inside a longer subject ID)."""
    return re.compile(rf"(?<![A-Za-z0-9])((?:sub-)?{re.escape(subject)}_ses-)([A-Za-z0-9]+)")


def _new_name(path: Path, subject: str, mapping: dict[str, str]) -> str:
    name = path.name
    if path.parent.name == f"sub-{subject}" and name.startswith("ses-"):
        label = name.removeprefix("ses-")
        return f"ses-{mapping[label]}" if label in mapping else name
    return _session_token(subject).sub(
        lambda m: m.group(1) + mapping.get(m.group(2), m.group(2)), name
    )


def _rename_tree(root: Path, subject: str, mapping: dict[str, str]) -> list[Path]:
    """Rename everything under root carrying one of mapping's labels; deepest first, so a
    folder's contents are renamed before the folder."""
    renamed = []
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        new = _new_name(path, subject, mapping)
        if new != path.name:
            target = path.with_name(new)
            if target.exists():
                raise FileExistsError(f"{target} already exists; not renaming {path.name}")
            path.rename(target)
            renamed.append(target)
    return renamed


def rename_outputs(
    bids_root: Path, output_root: Path, subject: str, changes: list[SessionChange]
) -> list[Path]:
    """Rename the subject's C-PAC output and log files to the new session labels, then
    record the current BIDS sessions. Returns the renamed paths (final names).

    The working folders are not renamed here: their cached steps belong to the old BIDS
    paths, so they should be cleared instead (cpac_docker.clear_working_volume).
    """
    to_temporary = {c.old: f"{c.new}{RENAMING}" for c in changes}
    from_temporary = {f"{c.new}{RENAMING}": c.new for c in changes}
    roots = [output_root / "output", output_root / "log"]
    for root in roots:
        if root.is_dir():
            _rename_tree(root, subject, to_temporary)
    renamed = [
        p for root in roots if root.is_dir() for p in _rename_tree(root, subject, from_temporary)
    ]
    record_sessions(bids_root, output_root, subject)
    log.info("sub-%s: renamed %d output path(s): %s", subject, len(renamed),
             ", ".join(map(str, changes)))  # fmt: skip
    return renamed
