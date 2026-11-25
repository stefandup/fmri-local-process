"""fmri_behav_check: which behavioural log goes with which scan session, and what's off.

Thin report around processing/behav_match.py -- the same matching the converter uses. Read
only: decisions are made in the toolbox (window or terminal) and saved in
the BIDS folder.
"""

import csv
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from fmri_local_process.app.behav import (
    COLUMNS,
    S_NOTES,
    S_SESSION,
    BehavWork,
    event_rows,
    fit_text,
    load_behav_data,
    log_cells,
    log_title,
)
from fmri_local_process.processing.behav_logs import read_log_lines
from fmri_local_process.processing.behav_match import (
    ABORTED,
    ISO_DATE,
    MATCHED,
    UNREADABLE,
    LogMatch,
    events_summary,
    inventory,
    needs_review,
)
from fmri_local_process.processing.crosscheck import bold_run_length

console = Console()
FOLDER = click.Path(exists=True, file_okay=False, path_type=Path)
STATUS_STYLE = {MATCHED: "green"}
STATE_STYLE = {ABORTED: "yellow", UNREADABLE: "red"}
TABLE_WIDTH = 150  # narrower terminals get a block per log: a folded table can't be read

EXAMPLES_EPILOG = """
Examples:

\b
  Every log against every scan session, with the corrections saved in the BIDS folder:
  fmri_behav_check path/to/logs path/to/dicoms --bids path/to/bids

\b
  Only what needs a look:
  fmri_behav_check path/to/logs path/to/dicoms --bids path/to/bids --problems-only

\b
  One log's events (its path as listed), and whether they fit its BOLD run:
  fmri_behav_check path/to/logs path/to/dicoms --bids path/to/bids \\
    --log emolabeling/2024/P001-....log

Fix what it finds in the toolbox (fmri_toolbox, or fmri_tui without a display): Raw DICOMs and
Behaviour logs.
"""


def date_text(m: LogMatch) -> str:
    info = m.info
    if info.found_date is None:
        return "-"
    return f"{info.time_text} ({info.date_source})"


def read_work(behav_root: Path, dicom_root: Path, bids_root: Path | None) -> BehavWork:
    """As the Raw DICOMs and Behaviour logs tabs read them, with every correction saved in the
    BIDS folder (none without one)."""
    bids = bids_root or dicom_root / "(no BIDS folder)"  # isn't there: nothing saved in it
    return BehavWork(bids, behav_root, dicom_root, load_behav_data(bids, behav_root, dicom_root))


def print_folders(work: BehavWork, problems_only: bool) -> None:
    """The DICOM session folders: what they resolve to, and what is wrong with them."""
    console.print("DICOM session folders", style="bold")
    shown = 0
    for row, (folder, _tasks) in enumerate(work.scanned):
        cells = work.folder_cells(row)
        if problems_only and not cells[S_NOTES]:
            continue
        shown += 1
        console.print(
            f"{folder.name} -> {cells[S_SESSION] or 'not converted'}", markup=False, highlight=False
        )
        if cells[S_NOTES]:
            console.print(f"   {cells[S_NOTES]}", style="yellow", markup=False, highlight=False)
    if not shown:
        console.print("   no problems")


def notes_text(m: LogMatch) -> str:
    return "; ".join(filter(None, [m.hint, *m.info.notes, m.info.correction.note]))


def print_matches(matches: list[LogMatch]) -> None:
    if console.width < TABLE_WIDTH:
        for m in matches:
            print_block(m)
        return
    table = Table(title="Behavioural logs vs scan sessions")
    for column in ("Log", "Task", "ID", "Date", "Match", "Session", "Log state", "Notes"):
        table.add_column(column, overflow="fold")
    for m in matches:
        info = m.info
        style = STATUS_STYLE.get(m.status, "red")
        subject = (
            f"{info.subject} (was {info.file_subject})" if info.correction.subject else info.subject
        )
        verdict = f" [{info.correction.review}]" if info.correction.review else ""
        table.add_row(
            info.key,
            info.task.task if info.task else "?",
            subject,
            date_text(m),
            f"[{style}]{m.status}[/]{verdict}",
            m.session.name if m.session else "",
            f"[{STATE_STYLE.get(info.state, 'green')}]{info.state}",
            notes_text(m),
        )
    console.print(table)


def print_block(m: LogMatch) -> None:
    info = m.info
    session = f" -> {m.session.name}" if m.session else ""
    verdict = f" [{info.correction.review}]" if info.correction.review else ""
    console.print(info.key, style="bold", markup=False, highlight=False)
    console.print(
        f"   {m.status}{session}{verdict}", style=STATUS_STYLE.get(m.status, "red"), markup=False
    )
    task = info.task.task if info.task else "?"
    console.print(
        f"   {task}, ID {info.subject}, {date_text(m)}, {info.state}", markup=False, highlight=False
    )
    if notes := notes_text(m):
        console.print(f"   {notes}", style="yellow", markup=False, highlight=False)


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


def print_log(m: LogMatch, bids_root: Path | None) -> None:
    """One log's events, as the Behaviour logs tab shows the selected one."""
    print_block(m)
    console.print(f"   {log_title(m)}", markup=False, highlight=False)
    if m.info.task is None:
        raise click.ClickException("No task: no events to show.")
    try:
        events = m.info.task.extract(read_log_lines(m.info.path))
    except (OSError, ValueError, KeyError, IndexError) as e:
        raise click.ClickException(f"Can't extract events: {e!r}") from e
    run = None
    if bids_root is not None and m.session is not None:
        run = bold_run_length(bids_root, m.info.subject, m.session.label, m.info.task.task)
    text, color = fit_text(events, run)
    console.print(f"   {text}", style=color, markup=False, highlight=False)
    for line in events_summary(events):
        console.print(line, markup=False, highlight=False)
    table = Table("onset (s)", "duration (s)", "trial_type", events.modulator)
    for row in event_rows(events):
        table.add_row(*row)
    console.print(table)


def write_csv(matches: list[LogMatch], path: Path) -> None:
    """The Behaviour logs tab's table."""
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for m in matches:
            correction = m.info.correction
            day = correction.date.strftime(ISO_DATE) if correction.date else ""
            cells = log_cells(m) | {4: correction.subject or "", 5: day}
            writer.writerow([cells.get(i, "") for i in range(len(COLUMNS))])


@click.command(epilog=EXAMPLES_EPILOG)
@click.version_option(package_name="fmri-local-process")
@click.argument("behav_root", type=FOLDER, required=True)
@click.argument("dicom_root", type=FOLDER, required=True)
@click.option(
    "--bids",
    "bids_root",
    type=FOLDER,
    help="BIDS folder whose saved corrections (logs and DICOM folders) to apply.",
)
@click.option(
    "--problems-only",
    is_flag=True,
    help="List only logs that need review: not matched, incomplete or with notes, and no "
    "verdict (OK / error / not used) recorded in the crosscheck yet.",
)
@click.option("--log", "log_key", help="Show one log's events (its path as listed).")
@click.option("--csv", "csv_path", type=click.Path(path_type=Path), help="Also save the list.")
def main(
    behav_root: Path,
    dicom_root: Path,
    bids_root: Path | None,
    problems_only: bool,
    log_key: str | None,
    csv_path: Path | None,
) -> None:
    """Match every behavioural log in BEHAV_ROOT to the DICOM sessions in DICOM_ROOT.

    A log matches a session by subject ID (file name before the first '-') and date (the
    YYYYMMDD of <ID>-<task>_scan-YYYYMMDD-HHMMSS-<uid>.log, else the log's "Logfile
    written" line). Logs whose ID has scans on other days, IDs with no scans at all (with
    same-day sessions that still lack a log), duplicates, and logs that stop before the
    thank-you screen are all listed, followed
    by the trial types and column layouts found per task. Verdicts made in the crosscheck
    (OK, error, not used) are shown; error and not used logs are left out of BIDS. Nothing
    is written, but the --csv file.
    """
    with console.status("Reading logs and scan sessions..."):
        work = read_work(behav_root, dicom_root, bids_root)
    matches = work.matches
    if log_key is not None:
        key = Path(log_key).as_posix()
        match = next((m for m in matches if m.info.key == key), None)
        if match is None:
            raise click.ClickException(f"No such log: {log_key}")
        print_log(match, bids_root)
        return
    print_folders(work, problems_only)
    console.print()
    shown = [m for m in matches if not problems_only or needs_review(m)]
    print_matches(shown)
    print_inventory(matches)
    matched = sum(m.status == MATCHED for m in matches)
    waiting = sum(needs_review(m) for m in matches)
    marked = sum(m.info.correction.crosschecked for m in matches)
    console.print(
        f"{matched} of {len(matches)} log(s) matched to a session; {waiting} need review; "
        f"{marked} crosschecked."
    )
    if csv_path is not None:
        write_csv(shown, csv_path)
        console.print(f"Saved {csv_path}", markup=False)


if __name__ == "__main__":
    main()  # pyright: ignore[reportCallIssue] -- click supplies the arguments
