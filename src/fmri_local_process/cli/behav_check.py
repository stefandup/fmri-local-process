"""fmri_behav_check: which behavioural log goes with which scan session, and what's off.

Thin report around processing/behav_match.py -- the same matching the converter uses. Read
only: corrections are made in the crosscheck GUI ("Behaviour logs...") and saved in the BIDS
folder.
"""

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from fmri_local_process.processing.behav_match import (
    ABORTED,
    MATCHED,
    UNREADABLE,
    LogMatch,
    inspect_logs,
    inventory,
    match_logs,
    needs_review,
    read_corrections,
)
from fmri_local_process.processing.philani_bids import scan_sessions

console = Console()
STATUS_STYLE = {MATCHED: "green"}
STATE_STYLE = {ABORTED: "yellow", UNREADABLE: "red"}

EXAMPLES_EPILOG = """
Examples:

\b
  Every log against every scan session, with corrections saved in the BIDS folder:
  fmri_behav_check D:\\philani_behavioural_logs D:\\philani --bids D:\\philani_bids

\b
  Only the logs that need a look:
  fmri_behav_check D:\\philani_behavioural_logs D:\\philani --problems-only
"""


def date_text(m: LogMatch) -> str:
    info = m.info
    if info.date is None:
        return "-"
    text = info.time_text
    if info.correction.date:
        return f"{text} (corrected)"
    return f"{text} ({info.date_source})"


def print_matches(matches: list[LogMatch]) -> None:
    table = Table(title="Behavioural logs vs scan sessions")
    for column in ("Log", "Task", "ID", "Date", "Match", "Session", "Log state", "Notes"):
        table.add_column(column, overflow="fold")
    for m in matches:
        info = m.info
        style = STATUS_STYLE.get(m.status, "red")
        subject = (
            f"{info.subject} (was {info.file_subject})" if info.correction.subject else info.subject
        )
        notes = "; ".join(filter(None, [m.hint, *info.notes, info.correction.note]))
        verdict = f" [{info.correction.review}]" if info.correction.review else ""
        table.add_row(
            info.key,
            info.task.task if info.task else "?",
            subject,
            date_text(m),
            f"[{style}]{m.status}[/]{verdict}",
            m.session.name if m.session else "",
            f"[{STATE_STYLE.get(info.state, 'green')}]{info.state}",
            notes,
        )
    console.print(table)


def print_inventory(matches: list[LogMatch]) -> None:
    for task, inv in inventory([m.info for m in matches]).items():
        states = ", ".join(f"{n} {state}" for state, n in inv.states.most_common())
        table = Table(title=f"{task}: {inv.logs} log(s) -- {states}")
        table.add_column("Trial type")
        table.add_column("In how many logs", justify="right")
        for trial_type, n in inv.trial_types.most_common():
            table.add_row(trial_type, str(n))
        console.print(table)
        for n, (layout, count) in enumerate(inv.layouts.most_common(), start=1):
            console.print(f"  column layout {n} ({count} log(s), {len(layout)} columns): ", end="")
            console.print(", ".join(layout), markup=False)


@click.command(epilog=EXAMPLES_EPILOG)
@click.version_option(package_name="fmri-local-process")
@click.argument("behav_root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("dicom_root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--bids",
    "bids_root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="BIDS folder whose code/behav_corrections.json corrections to apply.",
)
@click.option(
    "--problems-only",
    is_flag=True,
    help="List only logs that need review: not matched, incomplete or with notes, and no "
    "verdict (OK / error / not used) recorded in the crosscheck yet.",
)
def main(behav_root: Path, dicom_root: Path, bids_root: Path | None, problems_only: bool) -> None:
    """Match every behavioural log in BEHAV_ROOT to the DICOM sessions in DICOM_ROOT.

    A log matches a session by subject ID (file name before the first '-') and date (the
    YYYYMMDD of <ID>-<task>_scan-YYYYMMDD-HHMMSS-<uid>.log, else the log's "Logfile
    written" line). Logs whose ID has scans on other days, IDs with no scans at all (with
    same-day sessions that still lack a log), duplicates, and logs that stop before the
    thank-you screen are all listed, followed
    by the trial types and column layouts found per task. Verdicts made in the crosscheck
    (OK, error, not used) are shown; error and not used logs are left out of BIDS. Nothing
    is written.
    """
    corrections = read_corrections(bids_root) if bids_root else {}
    with console.status("Reading logs and scan sessions..."):
        infos = inspect_logs(behav_root, corrections)
        matches = match_logs(infos, scan_sessions(dicom_root))
    shown = [m for m in matches if not problems_only or needs_review(m)]
    print_matches(shown)
    print_inventory(matches)
    matched = sum(m.status == MATCHED for m in matches)
    console.print(f"{matched} of {len(matches)} log(s) matched to a session.")


if __name__ == "__main__":
    main()  # pyright: ignore[reportCallIssue] -- click supplies the arguments
