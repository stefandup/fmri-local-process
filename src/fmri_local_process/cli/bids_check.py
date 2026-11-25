"""fmri_bids_check: go through a converted BIDS folder: each session's problems, the scan lengths
and (with --dicom) missing volumes, as the crosscheck tabs show them. Read only; record decisions
with fmri_bids_fix."""

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from fmri_local_process.app import dicom_volumes as adv
from fmri_local_process.app.crosscheck import ICONS, needs_a_look, problem_text, scanned_text
from fmri_local_process.app.overview import summary_lines
from fmri_local_process.app.scan_lengths import ScanLengths, length_note, trimmed_note
from fmri_local_process.cli.folders import find_folders
from fmri_local_process.processing import dicom_volumes as dv
from fmri_local_process.processing.action_log import read_actions
from fmri_local_process.processing.crosscheck import (
    SessionReview,
    find_data_files,
    read_expected_sessions,
    review_bids_folder,
    sidecar_note,
    write_overview_csv,
)
from fmri_local_process.processing.philani_bids import read_excluded_sessions

console = Console()
FOLDER = click.Path(exists=True, file_okay=False, path_type=Path)
EPILOG = """
Examples:

\b
  fmri_bids_check                      what still needs a look (the folder set with fmri_folders)
  fmri_bids_check --all --files        every session, with its files
  fmri_bids_check --lengths            expected lengths, and runs to trim
  fmri_bids_check --volumes            volumes missing from the NIfTIs or the DICOMs
  fmri_bids_check --csv overview.csv
  fmri_bids_check D:\\other_bids       another folder
"""


def say(text: str, style: str = "") -> None:
    """Names are shown as they are, never as markup."""
    console.print(text, style=style or None, markup=False, highlight=False)


def print_session(review: SessionReview, bids_root: Path, files: bool) -> None:
    say(f"{review.name}  {problem_text(review)}", "bold")
    say(f"   {scanned_text(review)}" + (f"   note: {review.note}" if review.note else ""))
    for issue in review.issues:
        say(f"   {ICONS[issue.kind]} {issue.text}")
    for data in review.pending:
        where = data.path.relative_to(bids_root).as_posix()
        say(f"     keep one: {where}   {data.summary()}   {sidecar_note(data)}")
    for data in review.files if files else []:
        say(f"     {data.path.relative_to(bids_root).as_posix()}   {data.summary()}")


def print_lengths(bids_root: Path) -> None:
    lengths = ScanLengths(bids_root, find_data_files(bids_root))
    table = Table(
        "Scan", "Runs", "Found", "Expected", "From logs", "Minutes", "Too long", "Trimmed"
    )
    for label, runs in lengths.scans.items():
        table.add_row(
            label,
            str(len(runs)),
            length_note(runs),
            str(lengths.set[label] or lengths.automatic_text(label)),
            lengths.from_logs(label)[0],
            lengths.minutes(label),
            str(len(lengths.too_long(label)) or ""),
            trimmed_note(runs),
        )
    console.print(table)
    for label in lengths.scans:
        if details := lengths.too_long_details(label):
            say(f"{label}, expected {lengths.expected(label)}:\n{details}")


def print_volumes(bids_root: Path, dicom_root: Path) -> None:
    runs = dv.bold_runs(find_data_files(bids_root))
    with console.status("Reading DICOMs...") as status:
        job = adv.CheckJob(
            bids_root,
            dicom_root,
            runs,
            lambda done, total: status.update(f"Reading DICOMs: run {done} of {total}..."),
        )
        checks, error = job.run()
    if error:
        raise click.ClickException(f"DICOMs not read: {error}")
    for check in checks:
        say(f"{check.name}: {check.result}", adv.RESULT_COLOR[check.result])
        if check.result != dv.OK:
            say(f"   {check.explanation}")
    say(adv.status_text(dicom_root, runs, checks))


@click.command(epilog=EPILOG)
@click.version_option(package_name="fmri-local-process")
@click.argument("bids_root", required=False, type=FOLDER)
@click.option("--all", "everything", is_flag=True, help="Also the sessions with nothing to do.")
@click.option("--files", is_flag=True, help="List each session's files.")
@click.option("--lengths", is_flag=True, help="Scan lengths: expected, found, too long.")
@click.option("--volumes", is_flag=True, help="Check the DICOMs for missing volumes.")
@click.option("--dicom", "dicom_root", type=FOLDER, help="With --volumes: the DICOM folder.")
@click.option("--actions", type=click.IntRange(1), help="The last changes made to this folder.")
@click.option("--csv", "csv_path", type=click.Path(path_type=Path), help="Save the overview.")
def main(
    bids_root: Path | None,
    everything: bool,
    files: bool,
    lengths: bool,
    volumes: bool,
    dicom_root: Path | None,
    actions: int | None,
    csv_path: Path | None,
) -> None:
    """Check the BIDS folder BIDS_ROOT: what is missing, short, too long or still to pick.

    Nothing is changed (but the --csv file is written).
    """
    wanted = {"bids": bids_root, "dicom": dicom_root} if volumes else {"bids": bids_root}
    roots, _more, _remembered = find_folders(wanted)
    bids_root = roots["bids"]
    reviews = [r for r in review_bids_folder(bids_root) if r.session is not None]
    excluded = len(read_excluded_sessions(bids_root))
    for line in summary_lines(reviews, read_expected_sessions(bids_root), excluded):
        say(line)
    for review in reviews:
        if everything or needs_a_look(review):
            print_session(review, bids_root, files)
    if lengths:
        print_lengths(bids_root)
    if volumes:
        print_volumes(bids_root, roots["dicom"])
    for action in list(reversed(read_actions(bids_root)))[: actions or 0]:
        say("  ".join(str(value) for value in action.values()))
    if csv_path is not None:
        write_overview_csv(reviews, csv_path)
        say(f"Saved {csv_path}")


if __name__ == "__main__":
    main()  # pyright: ignore[reportCallIssue] -- click supplies the arguments
