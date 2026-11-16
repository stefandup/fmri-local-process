"""fmri_convert_to_bids: convert a folder of Philani DICOM sessions to BIDS."""

import logging
import sys
from functools import partial
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from fmri_local_process.processing.dcm2niix import (
    Dcm2niixNotFoundError,
    find_dcm2niix,
    run_dcm2niix,
)
from fmri_local_process.processing.deface import DefaceUnavailableError, make_deface
from fmri_local_process.processing.philani_bids import (
    ConversionReport,
    NotEnoughSpaceError,
    convert_dicom_root,
    reset_bids_root,
)

console = Console()

EXAMPLES_EPILOG = """
Examples:

\b
  Convert everything, with behavioural logs (defaced with the installer's Docker defacer):
  fmri_convert_to_bids D:\\philani D:\\philani_bids --behav-logs D:\\behav_logs

\b
  Deface with your own mri_deface + templates (or a FreeSurfer home) instead:
  fmri_convert_to_bids D:\\philani D:\\philani_bids --freesurfer D:\\tools\\mri_deface

\b
  Rebuild an old flat-layout BIDS folder from scratch, without prompts:
  fmri_convert_to_bids D:\\philani D:\\philani_bids --behav-logs D:\\behav_logs --reset -y

\b
  Convert one session folder that doesn't follow the naming rules, as sub-PILOT01:
  fmri_convert_to_bids D:\\scans\\20261020_1522_PILOT D:\\pilot_bids --subject PILOT01 --no-deface
"""


def print_report(report: ConversionReport) -> None:
    table = Table(title="BIDS conversion")
    table.add_column("Result")
    table.add_column("Count", justify="right")
    table.add_row("converted/updated", str(len(report.converted)))
    table.add_row("[yellow]repeated runs to pick at crosscheck", f"[yellow]{len(report.to_pick)}")
    table.add_row("[yellow]logs not placed in a session", f"[yellow]{len(report.unplaced)}")
    table.add_row("[yellow]sessions renumbered", f"[yellow]{len(report.renumbered)}")
    table.add_row("[yellow]converted from incomplete data", f"[yellow]{len(report.flagged)}")
    table.add_row("skipped sessions (>1 GB)", str(len(report.skipped)))
    table.add_row("sessions left out (deleted at crosscheck)", str(len(report.excluded)))
    table.add_row("[yellow]missing behavioural logs", f"[yellow]{len(report.missing)}")
    table.add_row("[red]failed", f"[red]{len(report.failures)}")
    console.print(table)
    for what, why in report.flagged:
        console.print(f"[yellow]INCOMPLETE[/yellow] {what}: {why}")
    for change in report.renumbered:
        console.print(f"[yellow]RENUMBERED[/yellow] {change} (a session was added or deleted)")
    for folder in report.excluded:
        console.print(f"LEFT OUT {folder} (deleted at crosscheck: code/excluded_sessions.json)")
    for log_key, why in report.unplaced:
        console.print(f"[yellow]NOT PLACED[/yellow] {log_key}: {why}")
    for what in report.missing:
        console.print(f"[yellow]MISSING[/yellow] {what} log")
    for what, why in report.failures:
        console.print(f"[red]FAILED[/red] {what}: {why}")


@click.command(epilog=EXAMPLES_EPILOG)
@click.version_option(package_name="fmri-local-process")
@click.argument("dicom_root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("bids_root", type=click.Path(file_okay=False, path_type=Path))
@click.option("--no-deface", is_flag=True, help="Skip T1w face removal.")
@click.option(
    "--freesurfer",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Deface with the mri_deface in this folder: a FreeSurfer home, or a folder holding "
    "mri_deface, talairach_mixed_with_skull.gca and face.gca. Default: $FREESURFER_HOME, "
    "else the defacer in the installer-built philani_fmri_process Docker image.",
)
@click.option(
    "--reset",
    is_flag=True,
    help="First delete sub-*, crosscheck_pending/ (older versions) and the crosscheck JSON "
    "files in BIDS_ROOT.",
)
@click.option(
    "--behav-logs",
    "behav_root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Folder searched recursively for behavioural *.log files (task from "
    "'cogreappraisal'/'emolabeling' in the path) -> events.tsv. May be DICOM_ROOT itself.",
)
@click.option(
    "--dcm2niix",
    "dcm2niix_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="dcm2niix executable to use instead of the bundled/fetched one.",
)
@click.option(
    "--subject",
    "subject_id",
    help="Bypass the folder-name rules: DICOM_ROOT is itself ONE session, converted as "
    "sub-<SUBJECT>/ses-1 (non-alphanumerics are dropped).",
)
@click.option("-y", "--yes", is_flag=True, help="Don't prompt (create BIDS_ROOT, confirm reset).")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def main(
    dicom_root: Path,
    bids_root: Path,
    no_deface: bool,
    freesurfer: Path | None,
    reset: bool,
    behav_root: Path | None,
    dcm2niix_path: Path | None,
    subject_id: str | None,
    yes: bool,
    verbose: bool,
) -> None:
    """Convert DICOM_ROOT (one folder per scan session) into a BIDS dataset at BIDS_ROOT.

    Every session is written as sub-<id>/ses-<n>/, n = 1, 2, ... in calendar order of the
    YYYYMMDD date in the session folder name (sub-<id>_sessions.tsv records which folder each
    session came from; when an earlier session turns up, the later ones are renumbered).
    A scan repeated in one session is converted as run-1, run-2, ...: keep one at crosscheck.
    A log goes into the session its date matches (or the only one); logs that fit nowhere
    are listed as NOT PLACED -- sort them out in the crosscheck's IDs & behaviour logs.

    With --behav-logs, every subject is expected to have a Reappraisal and a Matching log;
    a missing one is logged and listed as MISSING, and conversion carries on. events.tsv
    onset/duration are in seconds, onset from the first scanner pulse (BIDS convention).

    \b
    NOTE: a BIDS_ROOT written by the old philaniToBIDS.sh (or an earlier version of this
    tool) uses the flat sub-<id>/anat layout without ses-<n>/. Re-run with --reset to
    rebuild it in the session layout.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )

    try:
        exe = find_dcm2niix(dcm2niix_path)
        deface = None if no_deface else make_deface(freesurfer)
    except (Dcm2niixNotFoundError, DefaceUnavailableError) as e:
        raise click.ClickException(str(e)) from e
    logging.info("Using dcm2niix at %s", exe)
    if deface is None:
        logging.warning("--no-deface: T1w images will NOT be defaced")
    if behav_root is None:
        logging.warning("No --behav-logs: events.tsv files will NOT be created")

    if not bids_root.exists():
        if not yes:
            click.confirm(f"{bids_root} does not exist. Create it?", abort=True)
        bids_root.mkdir(parents=True)

    if reset:
        if not yes:
            click.confirm(f"Delete all sub-* folders in {bids_root}?", abort=True)
        reset_bids_root(bids_root)

    try:
        report = convert_dicom_root(
            dicom_root, bids_root, partial(run_dcm2niix, exe), deface, behav_root, subject_id
        )
    except (ValueError, NotEnoughSpaceError) as e:  # unusable --subject, or the disk is full
        raise click.ClickException(str(e)) from e
    print_report(report)
    if report.to_pick or report.unplaced:
        console.print("[yellow]Pick runs / place logs in the crosscheck (fmri_crosscheck).")
    sys.exit(1 if report.failures else 0)


if __name__ == "__main__":
    main()  # pyright: ignore[reportCallIssue] -- click supplies the arguments
