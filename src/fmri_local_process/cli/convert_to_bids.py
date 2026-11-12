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
from fmri_local_process.processing.deface import DefaceUnavailableError, make_mri_deface
from fmri_local_process.processing.philani_bids import (
    ConversionReport,
    convert_dicom_root,
    reset_bids_root,
)

console = Console()


def print_report(report: ConversionReport) -> None:
    table = Table(title="BIDS conversion")
    table.add_column("Result")
    table.add_column("Count", justify="right")
    table.add_row("converted/updated series", str(len(report.converted)))
    table.add_row("skipped subjects (>1 GB)", str(len(report.skipped)))
    table.add_row("[red]failed series", f"[red]{len(report.failures)}")
    console.print(table)
    for what, why in report.failures:
        console.print(f"[red]FAILED[/red] {what}: {why}")


@click.command()
@click.version_option(package_name="fmri-local-process")
@click.argument("dicom_root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("bids_root", type=click.Path(file_okay=False, path_type=Path))
@click.option("--no-deface", is_flag=True, help="Skip T1w face removal (needs FreeSurfer).")
@click.option(
    "--reset",
    is_flag=True,
    help="Delete sub-* folders, crosscheck.json and selected_runs.json in BIDS_ROOT first.",
)
@click.option(
    "--dcm2niix",
    "dcm2niix_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="dcm2niix executable to use instead of the bundled/fetched one.",
)
@click.option("-y", "--yes", is_flag=True, help="Don't prompt (create BIDS_ROOT, confirm reset).")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def main(
    dicom_root: Path,
    bids_root: Path,
    no_deface: bool,
    reset: bool,
    dcm2niix_path: Path | None,
    yes: bool,
    verbose: bool,
) -> None:
    """Convert DICOM_ROOT (one folder per scan session) into a BIDS dataset at BIDS_ROOT."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )

    try:
        exe = find_dcm2niix(dcm2niix_path)
        deface = None if no_deface else make_mri_deface()
    except (Dcm2niixNotFoundError, DefaceUnavailableError) as e:
        raise click.ClickException(str(e)) from e
    logging.info("Using dcm2niix at %s", exe)
    if deface is None:
        logging.warning("--no-deface: T1w images will NOT be defaced")

    if not bids_root.exists():
        if not yes:
            click.confirm(f"{bids_root} does not exist. Create it?", abort=True)
        bids_root.mkdir(parents=True)

    if reset:
        if not yes:
            click.confirm(f"Delete all sub-* folders in {bids_root}?", abort=True)
        reset_bids_root(bids_root)

    report = convert_dicom_root(dicom_root, bids_root, partial(run_dcm2niix, exe), deface)
    print_report(report)
    sys.exit(1 if report.failures else 0)


if __name__ == "__main__":
    main()  # pyright: ignore[reportCallIssue] -- click supplies the arguments
