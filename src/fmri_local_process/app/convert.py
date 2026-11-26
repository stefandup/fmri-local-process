"""Setup & Convert to BIDS: the conversion job, the defacer check and the texts."""

import logging
from collections.abc import Callable
from functools import partial
from pathlib import Path

from fmri_local_process.app.colors import ERROR_COLOR, WARNING_COLOR
from fmri_local_process.app.state import ToolboxState
from fmri_local_process.processing.crosscheck import replay_decisions
from fmri_local_process.processing.dcm2niix import (
    Dcm2niixNotFoundError,
    find_dcm2niix,
    run_dcm2niix,
)
from fmri_local_process.processing.deface import (
    DefaceUnavailableError,
    DockerNeededError,
    make_deface,
)
from fmri_local_process.processing.philani_bids import ConversionReport, convert_dicom_root

PACKAGE_LOGGER = "fmri_local_process"
MAX_SESSIONS = 20
DOCKER_STOPPED, DOCKER_MISSING = "stopped", "missing"
DEFACER_WARNINGS = {
    DOCKER_STOPPED: "⚠ Docker isn't running: can't check for the defacer",
    DOCKER_MISSING: "⚠ Docker not installed: T1w won't be defaced",
    "": "⚠ No defacer: T1w won't be defaced",
}


def report_rows(report: ConversionReport) -> list[tuple[str, int, str]]:
    """(what, how many, colour) for the summary table, as the CLI prints it."""
    return [
        ("converted / updated", len(report.converted), ""),
        ("repeated runs to pick at Crosscheck", len(report.to_pick), WARNING_COLOR),
        ("logs not placed in a session", len(report.unplaced), WARNING_COLOR),
        ("sessions renumbered", len(report.renumbered), WARNING_COLOR),
        ("converted from incomplete data", len(report.flagged), WARNING_COLOR),
        ("missing behavioural logs", len(report.missing), WARNING_COLOR),
        ("skipped sessions (>1 GB, done)", len(report.skipped), ""),
        ("sessions left out (deleted at Crosscheck)", len(report.excluded), ""),
        ("sessions with no recognised scans", len(report.no_scans), WARNING_COLOR),
        ("failed", len(report.failures), ERROR_COLOR),
    ]


def report_lines(report: ConversionReport) -> list[str]:
    lines = [f"INCOMPLETE  {what}: {why}" for what, why in report.flagged]
    lines += [f"RENUMBERED  {change}" for change in report.renumbered]
    lines += [f"LEFT OUT  {folder} (deleted at Crosscheck)" for folder in report.excluded]
    lines += [
        f"NO SCANS  {folder}: no series folder matches a scan rule (found: {found})"
        for folder, found in report.no_scans
    ]
    lines += [f"NOT PLACED  {key}: {why} -- see Behaviour logs" for key, why in report.unplaced]
    lines += [f"MISSING  {what} log" for what in report.missing]
    lines += [f"FAILED  {what}: {why}" for what, why in report.failures]
    return lines


def has_subfolders(folder: Path) -> bool:
    try:
        return any(child.is_dir() for child in folder.iterdir())
    except OSError:  # gone, or (OneDrive) not synced
        return False


def setup_notices(state: ToolboxState) -> list[str]:
    """What the Setup tab is missing: folders not chosen, or a DICOM folder with no sessions."""
    dicom, behav, bids = (state.folder(key) for key in ("dicom", "behav", "bids"))
    messages = []
    if dicom is None:
        messages.append("No DICOM sessions folder chosen: Browse for it above.")
    elif not has_subfolders(dicom):
        messages.append(f"No session folders in {dicom}: nothing to convert.")
    if behav is None:
        messages.append(
            "No behavioural logs folder chosen: the BOLD runs get no events files "
            "(Browse for it above if this study has logs)."
        )
    if bids is None:
        messages.append("No BIDS folder chosen: Browse for the folder to convert into.")
    return messages


HELP_TEXT = (
    "<b>Setup & Convert to BIDS</b><br>"
    "Choose the raw folders on the left and the BIDS folder they are converted into on the "
    "right; every tab works on these folders, and they are remembered.<br><br>"
    "<b>DICOM sessions (raw)</b>: one folder per scan session, named "
    "&lt;YYYYMMDD&gt;_&lt;time&gt;_&lt;subject ID&gt;. <b>Behavioural logs (raw)</b> "
    "(optional): the top folder of the logs; every .log below it is found, its task from "
    "'cogreappraisal' / 'emolabeling' in its path. Raw folders are only read, never changed."
    "<br><br>"
    "<b>Convert to BIDS</b> puts every session into its ses-&lt;n&gt; in date order, with the "
    "logs' events. Re-running is safe: existing files are kept, deleted ones are recreated "
    "from the raw data, and the corrections saved on the other tabs (IDs, logs, deleted "
    "sessions) are applied. Each scan is converted in a temporary folder and only moved into "
    "BIDS once it (and its defacing) worked.<br><br>"
    "<b>Scan rules...</b>: which DICOM series folders become which BIDS scan (the Philani "
    "protocol names, plus your own, e.g. a clinical scan's t1_mprage as the T1w).<br><br>"
    "<b>Sessions per subject</b>: how many visits this study has; a subject with more is "
    "flagged ⊕ on the BIDS Crosscheck tab, so a dud visit stands out."
)


class LineHandler(logging.Handler):
    """Relays the converter's log records as lines (called on the converting thread)."""

    def __init__(self, tell: Callable[[str], object]) -> None:
        super().__init__(logging.INFO)
        self.tell = tell
        self.setFormatter(logging.Formatter("%(levelname)s  %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.tell(self.format(record))


def check_defacer() -> tuple[str, str]:
    """(why there is no defacer, "" when there is one; DOCKER_* when Docker is why). May ask
    Docker, which takes a moment: call it off the UI thread."""
    try:
        make_deface()
    except DockerNeededError as e:
        return str(e), DOCKER_STOPPED if e.installed else DOCKER_MISSING
    except (DefaceUnavailableError, OSError) as e:
        return str(e), ""
    return "", ""


class ConvertJob:
    def __init__(
        self,
        dicom_root: Path,
        bids_root: Path,
        behav_root: Path | None,
        deface: bool,
        progress: Callable[[int, int, str], None],  # steps done, total, what is starting now
    ) -> None:
        self.dicom_root, self.bids_root, self.behav_root = dicom_root, bids_root, behav_root
        self.deface, self.progress = deface, progress

    def run(self) -> ConversionReport | str:
        """The report, or why it couldn't run."""
        try:
            exe = find_dcm2niix()
            deface = make_deface() if self.deface else None
            return convert_dicom_root(
                self.dicom_root,
                self.bids_root,
                partial(run_dcm2niix, exe),
                deface,
                self.behav_root,
                progress=self.progress,
            )
        except (Dcm2niixNotFoundError, DefaceUnavailableError, OSError, ValueError) as e:
            return str(e)


def restore_question(backup: Path, bids_root: Path) -> str:
    has_sessions = any(bids_root.glob("sub-*/ses-*"))
    warning = (
        "\n\nThis BIDS folder already has converted sessions. Restore is meant for an "
        "empty or freshly made BIDS folder: its own corrections, notes and settings are "
        "replaced by the saved ones."
        if has_sessions
        else ""
    )
    return (
        f"Restore the crosscheck saved in {backup}?\n\nIts corrections, deleted sessions, "
        "notes and settings go back into the BIDS folder, everything is converted again "
        "from raw, and then the kept runs and trims are replayed." + warning
    )


def replay(bids_root: Path) -> tuple[list[str], str, bool]:
    """Replay the restored kept runs and trims: (log lines, summary, whether all worked)."""
    applied, unresolved = replay_decisions(bids_root)
    lines = [f"REPLAYED  {line}" for line in applied]
    lines += [f"NOT REPLAYED  {line}" for line in unresolved]
    text = f"Restore finished: {len(applied)} kept run(s) / trim(s) replayed."
    if unresolved:
        text += f"\n{len(unresolved)} could not be (see the Activity Log)."
    return lines, text, not unresolved
