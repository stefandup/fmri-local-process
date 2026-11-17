"""Export the behavioural logs on their own, as BIDS events.tsv files, to a folder of choice.

The Behaviour logs tab's "Export logs..." -- events without converting any scans: for
a quick look at the behaviour, or to hand over. The corrections and verdicts on the tab
apply: corrected IDs and dates are used, and logs marked error or not used are left out.

Two layouts:

* ONE_FOLDER: every file side by side, sub-<id>_ses-<n>_task-<task>_events.tsv
* BIDS_LAYOUT: sub-<id>/ses-<n>/func/sub-<id>_ses-<n>_task-<task>_events.tsv

A log matched to a scan session gets that session's number. One that isn't (no session for
its ID or date, or one of several for a session) goes under unplaced/, named with its log
date (acq-YYYYMMDD) instead, so nothing is silently guessed -- or, exporting into the
BIDS folder itself (skip_unplaced), is left out, since an unplaced/ folder isn't valid BIDS.
Each events.tsv has a JSON sidecar naming its source log, as the converter writes.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fmri_local_process.processing.behav_logs import read_log_lines, write_events_tsv
from fmri_local_process.processing.behav_match import MATCHED, LogMatch

ONE_FOLDER = "one folder"
BIDS_LAYOUT = "bids"
UNPLACED_DIR = "unplaced"

ExportProgress = Callable[[int, int], None]  # (logs done, total)


@dataclass
class ExportReport:
    written: list[Path] = field(default_factory=list)
    left_out: list[tuple[str, str]] = field(default_factory=list)  # (log, why)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (log, error)


def target_for(match: LogMatch, out_root: Path, layout: str) -> Path:
    """Where a log's events.tsv goes (before making the name unique)."""
    info = match.info
    assert info.task is not None
    subject, task = f"sub-{info.subject}", info.task.task
    if match.status == MATCHED and match.session is not None:
        session = f"ses-{match.session.label}"
        name = f"{subject}_{session}_task-{task}_events.tsv"
        folder = out_root / subject / session / "func" if layout == BIDS_LAYOUT else out_root
        return folder / name
    when = f"_acq-{info.date:%Y%m%d}" if info.date else ""
    name = f"{subject}_task-{task}{when}_events.tsv"
    folder = out_root / UNPLACED_DIR
    return (folder / subject if layout == BIDS_LAYOUT else folder) / name


def unique(path: Path, taken: set[Path]) -> Path:
    """path, or with run-<k> before _events when this export already wrote that name."""
    if path not in taken:
        return path
    stem = path.name.removesuffix("_events.tsv")
    k = 2
    while (candidate := path.with_name(f"{stem}_run-{k}_events.tsv")) in taken:
        k += 1
    return candidate


def export_events(
    matches: list[LogMatch],
    out_root: Path,
    layout: str = ONE_FOLDER,
    progress: ExportProgress | None = None,
    skip_unplaced: bool = False,
) -> ExportReport:
    report = ExportReport()
    taken: set[Path] = set()
    for n, match in enumerate(matches, start=1):
        if progress is not None:
            progress(n - 1, len(matches))
        info = match.info
        if info.task is None:
            report.left_out.append((info.key, "no task in its path"))
            continue
        if info.excluded:
            report.left_out.append((info.key, f"marked '{info.correction.review}'"))
            continue
        if skip_unplaced and (match.status != MATCHED or match.session is None):
            report.left_out.append((info.key, "not matched to a scan session"))
            continue
        target = unique(target_for(match, out_root, layout), taken)
        try:
            table = info.task.extract(read_log_lines(info.path))
            target.parent.mkdir(parents=True, exist_ok=True)
            write_events_tsv(table, target)
        except (OSError, ValueError, KeyError, IndexError) as e:
            report.failed.append((info.key, repr(e)))
            continue
        sidecar: dict[str, str] = {"SourceLog": info.path.name, "SourceLogMatch": match.status}
        if info.date is not None:
            sidecar["SourceLogDate"] = info.date.isoformat()
        if info.corrected:
            sidecar["SourceLogCorrectedID"] = info.subject
            sidecar["SourceLogCorrectedDate"] = info.date.isoformat() if info.date else ""
        target.with_suffix(".json").write_text(
            json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
        )
        taken.add(target)
        report.written.append(target)
    if progress is not None:
        progress(len(matches), len(matches))
    return report
