"""Raw DICOMs and Behaviour logs: reading the logs and DICOM folders, the tables' texts, the
corrections (typed, checked, saved) and the events export."""

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fmri_local_process.app.colors import ERROR_COLOR, OK_COLOR, WARNING_COLOR
from fmri_local_process.app.crosscheck import CROSSCHECKED_ICON
from fmri_local_process.app.state import ToolboxState, paths_conflict
from fmri_local_process.processing.action_log import record_action
from fmri_local_process.processing.behav_export import (
    BIDS_LAYOUT,
    ONE_FOLDER,
    UNPLACED_DIR,
    ExportReport,
    export_events,
)
from fmri_local_process.processing.behav_logs import (
    BEHAV_TASKS,
    EventTable,
    id_problem,
    read_log_lines,
)
from fmri_local_process.processing.behav_match import (
    ISO_DATE,
    MATCHED,
    REVIEW_ERROR,
    REVIEW_NOT_USED,
    REVIEW_OK,
    Correction,
    LogInfo,
    LogMatch,
    SessionInfo,
    inspect_logs,
    inventory,
    match_logs,
    needs_review,
    read_corrections,
    same_day_rivals,
    write_corrections,
)
from fmri_local_process.processing.crosscheck import bold_run_length
from fmri_local_process.processing.philani_bids import (
    DATE_FORMAT,
    ScannedFolder,
    read_crosschecked_folders,
    read_dicom_corrections,
    read_dicom_date_corrections,
    read_dicom_session_corrections,
    read_excluded_sessions,
    read_scan_rules,
    scan_dicom_root,
    session_infos,
    write_crosschecked_folders,
    write_dicom_corrections,
    write_dicom_date_corrections,
    write_dicom_session_corrections,
)
from fmri_local_process.processing.scan_cache import ScanCache

WARN_COLOR = WARNING_COLOR
BAD_COLOR = ERROR_COLOR
COLUMNS = (
    "Log", "Task", "ID in name", "Logged", "Corrected ID", "Corrected date",
    "Verdict", "Match", "Session", "Log state", "Notes",
)  # fmt: skip
CORRECTED_ID, CORRECTED_DATE, VERDICT, MATCH, SESSION, STATE, NOTES = range(4, 11)
VERDICT_COLORS = {REVIEW_OK: OK_COLOR, REVIEW_ERROR: BAD_COLOR, REVIEW_NOT_USED: WARN_COLOR}
SESSION_COLUMNS = (
    "DICOM folder", "Scanned", "ID in folder", "Corrected ID", "Corrected date",
    "Set session", "Resolves to", "BIDS session", "Scans", "Logs", "Checked", "Notes",
)  # fmt: skip
S_CORRECTED, S_DATE, S_FIXED, S_RESOLVED, S_SESSION, S_TASKS, S_LOGS, S_CHECKED, S_NOTES = range(
    3, 12
)
Verdict = tuple[str, str]  # (review, note)
# Which logs the table shows; the first is the default.
ALL_LOGS = "All logs"
MATCHED_NEEDS_REVIEW = "Matched and needs review"
NEEDS_REVIEW_ONLY = "Needs review only"
MATCHED_ONLY = "Matched"
UNMATCHED_ONLY = "Unmatched"
LOG_VIEWS = (ALL_LOGS, MATCHED_NEEDS_REVIEW, NEEDS_REVIEW_ONLY, MATCHED_ONLY, UNMATCHED_ONLY)
ISO_PATTERN = re.compile(r"\d{0,4}(-\d{0,2}(-\d{0,2})?)?")  # YYYY-MM-DD as it is typed
MAX_SESSION = 99


def parse_date(text: str) -> dt.date | None:
    """'' -> None; 2024-04-03 or 20240403 -> date; anything else raises ValueError."""
    text = text.strip()
    if not text:
        return None
    for fmt in (ISO_DATE, "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{text!r} is not a date (YYYY-MM-DD)")


def folder_name_parts(name: str) -> tuple[str, str]:
    """(scan date/time, ID as written) from <YYYYMMDD>_<HHMM>_<ID>[...]; "-" where missing."""
    parts = name.split("_")
    try:
        day = dt.datetime.strptime(parts[0], DATE_FORMAT).strftime(ISO_DATE)
    except ValueError:
        day = "-"
    time = parts[1] if len(parts) > 1 and len(parts[1]) == 4 and parts[1].isdigit() else ""
    when = f"{day} {time[:2]}:{time[2:]}" if time and day != "-" else day
    return when, parts[2] if len(parts) > 2 else "-"


def id_text(raw: str, canonical: str) -> str:
    return raw if raw == canonical else f"{raw} -> {canonical}"


def inventory_text(infos: list[LogInfo]) -> str:
    lines = []
    for task, inv in inventory(infos).items():
        states = ", ".join(f"{n} {state}" for state, n in inv.states.most_common())
        lines.append(f"{task}: {inv.logs} log(s) -- {states}")
        types = ", ".join(f"{t} ({n})" for t, n in inv.trial_types.most_common())
        lines.append(f"  trial types (logs containing each): {types or '-'}")
        for n, (layout, count) in enumerate(inv.layouts.most_common(), start=1):
            lines.append(f"  column layout {n}: {count} log(s), {len(layout)} columns: ")
            lines.append("    " + ", ".join(layout))
        lines.append("")
    return "\n".join(lines)


def events_end(table: EventTable) -> float | None:
    """When the last event ends, in seconds from the first pulse."""
    if not table.onset or table.duration[-1] is None:
        return None
    return table.onset[-1] + table.duration[-1]


def fit_text(table: EventTable, run: tuple[int, float | None] | None) -> tuple[str, str]:
    """How the events line up with the converted BOLD run: (text, colour)."""
    end = events_end(table)
    end_text = f"events end at {end:.1f} s" if end is not None else "events have no end"
    if run is None:
        return f"{end_text}; the BOLD run isn't in the BIDS folder yet", WARN_COLOR
    volumes, tr = run
    if tr is None:
        return f"{end_text}; BOLD run has {volumes} volumes (no TR in its sidecar)", WARN_COLOR
    length = volumes * tr
    text = f"{end_text}; BOLD run is {volumes} volumes x {tr:g} s = {length:.1f} s"
    if end is not None and end > length + tr:
        return f"{text}: events run past the end of the scan", BAD_COLOR
    return text, OK_COLOR


@dataclass
class BehavData:
    """What the tab reads from disk -- the slow part, done on a worker thread."""

    infos: list[LogInfo]
    scanned: list[ScannedFolder]
    dicom_corrections: dict[str, str]
    excluded: set[str]  # DICOM folders deleted at crosscheck
    dicom_dates: dict[str, dt.date] = field(default_factory=dict)  # corrected scan dates
    dicom_sessions: dict[str, str] = field(default_factory=dict)  # session numbers set
    checked_folders: set[str] = field(default_factory=set)  # marked crosschecked


LoadProgress = Callable[[int, int, str], None]  # (done, total, what)


def load_behav_data(
    bids_root: Path,
    behav_root: Path,
    dicom_root: Path,
    progress: LoadProgress | None = None,
    fresh: bool = False,
) -> BehavData:
    """The logs and DICOM session folders, reading only what changed since the last time
    (the scan cache); fresh reads everything again."""

    def step(what: str) -> Callable[[int, int], None] | None:
        if progress is None:
            return None
        report = progress
        return lambda done, total: report(done, total, what)

    cache = ScanCache.open(behav_root, dicom_root)  # only what changed is read again
    if fresh:
        cache.logs, cache.dicom = {}, {}
    infos = inspect_logs(behav_root, read_corrections(bids_root), step("Reading logs"), cache.logs)
    scanned = scan_dicom_root(
        dicom_root, step("Scanning DICOM session folders"), read_scan_rules(bids_root), cache.dicom
    )
    cache.save()
    return BehavData(
        infos,
        scanned,
        read_dicom_corrections(bids_root),
        set(read_excluded_sessions(bids_root)),
        read_dicom_date_corrections(bids_root),
        read_dicom_session_corrections(bids_root),
        read_crosschecked_folders(bids_root),
    )


SESSIONS_HELP_TEXT = (
    "Every DICOM session folder, with its scan date and time (to tell twins apart) and the "
    "subject ID as written in its name. A twin's 12345-1 becomes 12345T1; other characters "
    "(e.g. a space) are removed and flagged.<br><br>"
    "Correct a wrong subject ID or scan date, or set a session number (e.g. 2 when the "
    "first visit is missing), then press <b>Re-check</b>: <b>Resolves to</b> and <b>BIDS "
    "session</b> show the result, <b>Logs</b> the behaviour logs matched to it. Once you have "
    f"been through a folder, <b>Mark crosschecked</b> (it then shows {CROSSCHECKED_ICON}); "
    "select several rows (Ctrl/Shift-click or drag) to mark them all at once.<br><br>"
    "<b>Save</b> keeps this (and the Behaviour logs tab's corrections) in the BIDS folder's "
    "code/ folder; Convert to BIDS applies it. Raw folders are never renamed."
)
HELP_TEXT = (
    "Each log is matched to a scan session by subject ID and date. A twin's 12345-1 becomes "
    "12345T1; other characters (e.g. a space) are removed and flagged.<br><br>"
    "Correct a wrong ID or date here, or a wrong subject ID in a DICOM folder name on the "
    "<b>Raw DICOMs</b> tab, and press <b>Re-check</b>. Select a log to see its events and "
    "mark it OK or as an error; of several logs on one day, pick the one to use. Once you "
    f"have been through a log, <b>Mark crosschecked</b> (it then shows {CROSSCHECKED_ICON}); "
    "select several rows (Ctrl/Shift-click or drag) to mark them all at once.<br><br>"
    "<b>Save</b> keeps all of this in the BIDS folder (code/behav_corrections.json and "
    "code/dicom_id_corrections.json), and Convert to BIDS applies it. Raw files are never "
    "renamed. Sessions already converted under an old ID stay in the BIDS folder until you "
    "remove them."
)
SAVED_TEXT = (
    "Corrections saved. Convert to BIDS (tab 1) to apply them.\n\n"
    "Only behaviour logs changed? Export logs... on the Behaviour logs tab writes their events "
    "files into the BIDS folder without converting everything again."
)
REFRESH_CACHE_TIP = (
    "This reloads everything: every log and DICOM folder is read again, not only what "
    "changed since last time (Reload)."
)
FIX_RED = "Fix the cells marked red (dates as YYYY-MM-DD, sessions as a number)."
# Where "Export logs..." can put the events files.
INTO_BIDS, INTO_OUTPUT, INTO_OTHER = "bids", "output", "other"
EXPORT_SUBFOLDER = "events"  # in C-PAC's output folder


def parse_session(text: str) -> str | None:
    """'' -> None; '02' -> '2'; anything but a whole number from 1 raises ValueError."""
    text = text.strip()
    if not text:
        return None
    if not (text.isascii() and text.isdigit() and 1 <= int(text) <= MAX_SESSION):
        raise ValueError(f"{text!r} is not a session number (1 to {MAX_SESSION})")
    return str(int(text))


def behav_folders(state: ToolboxState) -> tuple[Path, Path, Path] | None:
    """(BIDS, behavioural logs, DICOM sessions), or None when one isn't chosen."""
    bids_root, behav_root, dicom_root = (state.folder(k) for k in ("bids", "behav", "dicom"))
    if bids_root is None or behav_root is None or dicom_root is None:
        return None
    return bids_root, behav_root, dicom_root


def missing_folders_notice(state: ToolboxState) -> list[str]:
    names = {"dicom": "DICOM sessions", "behav": "behavioural logs", "bids": "BIDS"}
    missing = [name for key, name in names.items() if state.folder(key) is None]
    if not missing:
        return []
    return [
        f"No {' / '.join(missing)} folder chosen: choose it on the Setup & Convert "
        "to BIDS tab. This tab matches the logs to the scan sessions, so it needs all "
        "three."
    ]


@dataclass
class FolderFixes:
    """What is typed for the DICOM session folders, by folder name."""

    ids: dict[str, str]
    dates: dict[str, dt.date]
    sessions: dict[str, str]
    checked: set[str]


class BehavWork:
    """The logs and DICOM folders read for one set of folders, with the corrections and verdicts
    given so far. Nothing is written until save()."""

    def __init__(self, bids_root: Path, behav_root: Path, dicom_root: Path, data: BehavData):
        self.bids_root, self.behav_root, self.dicom_root = bids_root, behav_root, dicom_root
        self.output_root: Path | None = None  # C-PAC's output folder, if chosen: for the export
        self.bids_locked = False  # view-only package: never write into the BIDS folder
        self.events_cache: dict[str, EventTable | str] = {}  # key -> table, or why it failed
        self.behav_tasks = {t.task for t in BEHAV_TASKS}  # tasks that should have a log
        self.infos, self.scanned = data.infos, data.scanned
        self.excluded = data.excluded
        self.fixes = FolderFixes(
            dict(data.dicom_corrections),
            dict(data.dicom_dates),
            dict(data.dicom_sessions),
            set(data.checked_folders),
        )
        self.verdicts: dict[str, Verdict] = {
            info.key: (info.correction.review, info.correction.note)
            for info in self.infos
            if info.correction.review
        }
        self.crosschecked = {info.key for info in self.infos if info.correction.crosschecked}
        self.sessions: list[SessionInfo] = []
        self.matches: list[LogMatch] = []
        self.match()

    def match(self) -> None:
        """Number the sessions and match the logs with the corrections given so far."""
        fixes = self.fixes
        self.sessions = session_infos(
            self.scanned, fixes.ids, self.excluded, fixes.dates, fixes.sessions
        )
        self.matches = match_logs(self.infos, self.sessions)

    def set_corrections(self, typed: dict[str, tuple[str, dt.date | None]]) -> None:
        """{log key: (ID, date)} as typed, put together with the verdicts and marks."""
        for info in self.infos:
            subject, day = typed.get(info.key, ("", None))
            review, note = self.verdicts.get(info.key, (None, ""))
            info.correction = Correction(
                subject.strip() or None, day, review, note, info.key in self.crosschecked
            )

    def corrections(self) -> dict[str, Correction]:
        return {i.key: i.correction for i in self.infos if i.correction != Correction()}

    # ---- verdicts -----------------------------------------------------------------------------

    def set_verdict(self, key: str, review: str | None, note: str = "") -> None:
        if review:
            self.verdicts[key] = (review, note)
        else:
            self.verdicts.pop(key, None)

    def use_this_one(self, chosen: LogMatch, note: str) -> None:
        """Of several logs of one day: this one into BIDS, the others 'not used'."""
        self.set_verdict(chosen.info.key, REVIEW_OK, note)
        for rival in same_day_rivals(self.matches, chosen):
            if rival.info.correction.review == REVIEW_ERROR:
                continue  # already an error: keep that verdict and its note
            name = chosen.info.path.name
            self.set_verdict(rival.info.key, REVIEW_NOT_USED, f"{name} used instead")

    # ---- texts --------------------------------------------------------------------------------

    def summary(self) -> str:
        matched = sum(m.status == MATCHED for m in self.matches)
        marked = sum(m.info.correction.crosschecked for m in self.matches)
        return (
            f"{matched} of {len(self.matches)} log(s) matched to a session; {self.waiting()} "
            f"need review; {CROSSCHECKED_ICON} {marked} crosschecked"
        )

    def waiting(self) -> int:
        return sum(needs_review(m) for m in self.matches)

    def folder_cells(self, row: int) -> dict[int, str]:
        """A DICOM folder's row once matched: resolved ID, BIDS session, logs, notes."""
        folder, tasks = self.scanned[row]
        session = next((s for s in self.sessions if s.folder == folder.name), None)
        _, raw_id = folder_name_parts(folder.name)
        matched = [
            f"{m.info.task.task}: {m.info.time_text}"
            for m in self.matches
            if m.status == MATCHED
            and m.session is not None
            and m.info.task is not None
            and m.session.folder == folder.name
        ]
        missing = sorted(t for t in tasks if not any(x.startswith(f"{t}:") for x in matched))
        notes = [id_problem(raw_id)] if not self.fixes.ids.get(folder.name) else []
        if session is None:
            notes.append(
                "not converted: no date or ID in the folder name; give it a Corrected ID "
                "and a Corrected date, then Re-check and Save"
            )
        fixed = self.fixes.sessions.get(folder.name, "")
        if session is not None and fixed and session.label != fixed:
            notes.append(f"ses-{fixed} already set for another of its folders")
        if not tasks:
            notes.append(
                "no series matches a scan rule: nothing would be converted (the "
                "conversion summary lists its series folders)"
            )
        notes += [f"no {task} log" for task in missing if task in self.behav_tasks]
        return {
            S_RESOLVED: id_text(raw_id, session.subject) if session else "-",
            S_SESSION: session.name if session else "",
            S_LOGS: "; ".join(sorted(matched)),
            S_CHECKED: CROSSCHECKED_ICON if folder.name in self.fixes.checked else "",
            S_NOTES: "; ".join(filter(None, notes)),
        }

    def notices(self) -> list[str]:
        messages = []
        if not self.infos:
            messages.append(f"No behavioural logs (.log files) found in {self.behav_root}.")
        if not self.scanned:
            messages.append(f"No DICOM session folders found in {self.dicom_root}.")
        return messages

    # ---- one log ------------------------------------------------------------------------------

    def events_of(self, info: LogInfo) -> EventTable | str:
        if info.key not in self.events_cache:
            if info.task is None:
                self.events_cache[info.key] = "No task: no events to show."
            else:
                try:
                    self.events_cache[info.key] = info.task.extract(read_log_lines(info.path))
                except (OSError, ValueError, KeyError, IndexError) as e:
                    self.events_cache[info.key] = f"Can't extract events: {e!r}"
        return self.events_cache[info.key]

    def fit_of(self, m: LogMatch, events: EventTable) -> tuple[str, str]:
        """How the log's events line up with its converted BOLD run: (text, colour)."""
        run = None
        if m.session is not None and m.info.task is not None:
            run = bold_run_length(self.bids_root, m.info.subject, m.session.label, m.info.task.task)
        return fit_text(events, run)

    # ---- saving -------------------------------------------------------------------------------

    def save(self) -> None:
        # Keep corrections for logs and folders not listed right now.
        root, fixes = self.bids_root, self.fixes
        keys = {info.key for info in self.infos}
        folders = {folder.name for folder, _tasks in self.scanned}
        kept = {k: v for k, v in read_corrections(root).items() if k not in keys}
        write_corrections(root, kept | self.corrections())
        kept_ids = {k: v for k, v in read_dicom_corrections(root).items() if k not in folders}
        write_dicom_corrections(root, kept_ids | only(fixes.ids, folders))
        kept_dates = {
            k: v for k, v in read_dicom_date_corrections(root).items() if k not in folders
        }
        write_dicom_date_corrections(root, kept_dates | only(fixes.dates, folders))
        kept_sessions = {
            k: v for k, v in read_dicom_session_corrections(root).items() if k not in folders
        }
        write_dicom_session_corrections(root, kept_sessions | only(fixes.sessions, folders))
        kept_checked = read_crosschecked_folders(root) - folders
        write_crosschecked_folders(root, kept_checked | fixes.checked)

    # ---- export -------------------------------------------------------------------------------

    def export_destinations(self) -> dict[str, str]:
        """What the export can go into: {text shown: INTO_BIDS / INTO_OUTPUT / INTO_OTHER}."""
        choices = {}
        if not self.bids_locked:
            choices[f"The BIDS folder (tab 1): {self.bids_root}"] = INTO_BIDS
        if self.output_root is not None:
            out = self.output_root / EXPORT_SUBFOLDER
            choices[f"C-PAC's output folder: {out}"] = INTO_OUTPUT
        choices["Another folder..."] = INTO_OTHER
        return choices

    def export_problem(self, out_root: Path) -> str:
        """Why the events can't go into out_root, a folder other than BIDS ('' if they can)."""
        if paths_conflict(out_root, self.bids_root):
            return "Not in (or around) the BIDS folder: choose 'The BIDS folder' instead."
        for raw, what in ((self.behav_root, "behavioural logs"), (self.dicom_root, "DICOMs")):
            if paths_conflict(out_root, raw):
                return f"Not in (or around) the raw {what}."
        return ""

    def export(
        self,
        out_root: Path,
        layout: str,
        into_bids: bool,
        progress: Callable[[int, int], None] | None = None,
    ) -> ExportReport:
        report = export_events(self.matches, out_root, layout, progress, skip_unplaced=into_bids)
        if into_bids and report.written:
            record_action(
                self.bids_root,
                "export behaviour events",
                str(self.bids_root),
                f"{len(report.written)} events file(s) written",
            )
        return report


def only[T](values: dict[str, T], keys: set[str]) -> dict[str, T]:
    return {key: value for key, value in values.items() if key in keys}


EXPORT_LAYOUTS = {
    "One folder (sub-..._events.tsv side by side)": ONE_FOLDER,
    "BIDS folders (sub-.../ses-.../func/)": BIDS_LAYOUT,
}
EXPORT_INTO_BIDS_QUESTION = (
    "Write each matched log's events.tsv into its sub-/ses-/func folder, "
    "replacing the events file already there (Convert to BIDS writes the same "
    "files)? Logs not matched to a scan session are left out."
)


def export_text(report: ExportReport, out_root: Path, into_bids: bool) -> str:
    lines = [f"{len(report.written)} events file(s) written to {out_root}."]
    if report.left_out:
        why = "not matched to a session, " if into_bids else ""
        lines.append(f"{len(report.left_out)} left out ({why}marked error / not used, or no task).")
    if report.failed:
        lines.append(f"{len(report.failed)} failed:")
        lines += [f"  {log}: {why}" for log, why in report.failed[:10]]
    unplaced = sum(UNPLACED_DIR in p.parts for p in report.written)
    if unplaced:
        lines.append(f"{unplaced} not matched to a session: in {UNPLACED_DIR}/.")
    return "\n".join(lines)


def log_shown(m: LogMatch, view: str) -> bool:
    """Whether the log is listed under `view` (one of LOG_VIEWS)."""
    matched = m.status == MATCHED
    if view == MATCHED_NEEDS_REVIEW:
        return matched and needs_review(m)
    if view == NEEDS_REVIEW_ONLY:
        return needs_review(m)
    if view == MATCHED_ONLY:
        return matched
    if view == UNMATCHED_ONLY:
        return not matched
    return True


def log_cells(m: LogMatch) -> dict[int, str]:
    """A log's row (COLUMNS), without the two typed cells."""
    info = m.info
    marked = info.correction.crosschecked
    verdict = [CROSSCHECKED_ICON if marked else "", info.correction.review or ""]
    return {
        0: info.key,
        1: info.task.task if info.task else "?",
        2: id_text(info.raw_subject, info.file_subject),
        3: f"{info.time_text} ({info.date_source})" if info.found_date else "-",
        VERDICT: " ".join(filter(None, verdict)),
        MATCH: m.status,
        SESSION: f"{m.session.name} ({m.session.when})" if m.session else "",
        STATE: info.state,
        NOTES: "; ".join(filter(None, [m.hint, *info.notes, info.correction.note])),
    }


def log_title(m: LogMatch) -> str:
    """'<status> -> <session>, <state>: <notes>' of the selected log."""
    notes = "; ".join(filter(None, [m.hint, *m.info.notes])) or "no issues found"
    return f"{m.status}{' -> ' + m.session.name if m.session else ''}, {m.info.state}: {notes}"


def event_rows(events: EventTable) -> list[tuple[str, ...]]:
    """(onset, duration, trial type, modulator) as text, one per event."""
    rows = zip(
        events.onset, events.duration, events.trial_type, events.modulator_values, strict=True
    )
    return [
        tuple(f"{v:.3f}" if isinstance(v, float) else str(v or "n/a") for v in values)
        for values in rows
    ]
