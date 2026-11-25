"""fmri_generate_sample_data: dummy DICOM sessions and behavioural logs, with the same command line
as vr_lab_toolbox's ..._generate_sample_data. Thin wrapper around processing/dummy_data.py."""

import logging
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, TaskID
from rich.table import Table

from fmri_local_process.app.convert import ConvertJob, report_lines, report_rows
from fmri_local_process.cli.folders import find_folders
from fmri_local_process.processing.dummy_data import (
    BEHAV_DIR,
    DICOM_DIR,
    DUMMY_DATA_LOG_FILENAME,
    ERROR_TYPES,
    DummyDataError,
    DummyReport,
    generate_dummy_dataset,
)

console = Console()
FOLDER = click.Path(exists=True, file_okay=False, path_type=Path)
DEFAULT_N_CLEAN = 5

EXAMPLES_EPILOG = f"""
Examples:

\b
  {DEFAULT_N_CLEAN} clean participants, reproducible:
  fmri_generate_sample_data dummy --seed 42

\b
  Also add one participant per known error scenario (typos, duplicate or
  aborted logs, twins, missing scans, ...):
  fmri_generate_sample_data dummy --with-errors --seed 42

\b
  A dummy twin of a real study, without exposing its data: IDs and dates match,
  or don't, where the real ones do; everything else is made up. Of the
  folders set with fmri_folders (logs in every folder below are found):
  fmri_generate_sample_data dummy --reference

\b
  Of other folders, the first 5 participants, with the error scenarios too:
  fmri_generate_sample_data dummy --reference-behav <logs> \\
    --reference-dicom <dicoms> --n-reference 5 --with-errors

\b
  Generate straight into a BIDS folder in the same call (makes whole series):
  fmri_generate_sample_data dummy --with-errors --bids-folder dummy_bids

\b
  Every run writes "{DUMMY_DATA_LOG_FILENAME}" inside output_folder -- a plain-text,
  one-line-per-participant explanation of what each one's scenario is for.
"""


@click.command(epilog=EXAMPLES_EPILOG)
@click.version_option(package_name="fmri-local-process")
@click.argument("output_folder", type=click.Path(file_okay=False, path_type=Path), required=True)
@click.option(
    "--n-clean",
    type=click.IntRange(0),
    default=None,
    help="Number of well-formed dummy participants  "
    f"[default: {DEFAULT_N_CLEAN}; 0 with reference folders]",
)
@click.option(
    "--with-errors",
    is_flag=True,
    help=f"Also generate one participant per known error scenario: {', '.join(ERROR_TYPES)}",
)
@click.option("--seed", type=int, default=None, help="Seed for reproducible generation")
@click.option("--verbose", is_flag=True, help="Give verbose output")
@click.option(
    "--reference",
    is_flag=True,
    help="Make dummy twins of the DICOM sessions and logs folders set with fmri_folders.",
)
@click.option(
    "--reference-behav",
    type=FOLDER,
    default=None,
    help="Real behavioural logs to make dummy twins of: every .log in any folder below.",
)
@click.option(
    "--reference-dicom",
    type=FOLDER,
    default=None,
    help="Real DICOM session folders to make dummy twins of. Only names are looked at (with "
    "--images, numbers in the headers too).",
)
@click.option(
    "--n-reference",
    type=click.IntRange(1),
    default=None,
    help="Only the first so many participants of the reference folders.",
)
@click.option(
    "--keep-word",
    multiple=True,
    help="A word of the reference folders to keep as it is, repeatable, e.g. a scan's name in your "
    "own scan rules. Never a person's name.",
)
@click.option(
    "--images",
    is_flag=True,
    help="Make whole series that can be converted (small images). Without it every series is one "
    "image: enough for matching logs to sessions.",
)
@click.option(
    "--bids-folder",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="If given, also converts the freshly generated data into this BIDS folder -- generate and "
    "convert in one call instead of two. Implies --images.",
)
def main(
    output_folder: Path,
    n_clean: int | None,
    with_errors: bool,
    seed: int | None,
    verbose: bool,
    reference: bool,
    reference_behav: Path | None,
    reference_dicom: Path | None,
    n_reference: int | None,
    keep_word: tuple[str, ...],
    images: bool,
    bids_folder: Path | None,
) -> None:
    """Generate synthetic raw data in OUTPUT_FOLDER, a new folder: dicom/ and behav/.

    Made-up participants (DUMMY000, DUMMY001, ...), and, with --reference-behav and/or
    --reference-dicom, a dummy twin of each real log and session folder. The real data is only read;
    IDs, dates, times, answers, names and images are made up, the link to the real IDs is never
    written down, and the output is deleted if anything real is found in it. What it keeps is the
    shape: how many logs and sessions, the order of the dates, the logs' layout, the number and kind
    of their events, and which of them were answered.
    """
    logging.basicConfig(level=logging.INFO if verbose else logging.ERROR, format="%(message)s")
    if reference:
        roots, _more, _remembered = find_folders(
            {"behav": reference_behav, "dicom": reference_dicom}
        )
        reference_behav, reference_dicom = roots["behav"], roots["dicom"]
    has_reference = reference_behav is not None or reference_dicom is not None
    if n_reference is not None and not has_reference:
        raise click.UsageError(
            "--n-reference needs --reference, --reference-behav or --reference-dicom."
        )
    if n_clean is None:
        n_clean = 0 if has_reference else DEFAULT_N_CLEAN
    with Progress(console=console, transient=True) as bars:
        tasks: dict[str, TaskID] = {}

        def progress(what: str, done: int, total: int) -> None:
            if what not in tasks:
                tasks[what] = bars.add_task(what, total=total)
            bars.update(tasks[what], completed=done)

        try:
            report = generate_dummy_dataset(
                output_folder,
                n_clean,
                with_errors,
                reference_behav,
                reference_dicom,
                n_reference,
                images or bids_folder is not None,
                keep_word,
                seed,
                progress,
            )
        except (DummyDataError, OSError) as e:
            raise click.ClickException(str(e)) from e
    print_report(report, output_folder)
    if bids_folder is not None:
        convert(output_folder, bids_folder)


def print_report(report: DummyReport, output_folder: Path) -> None:
    """Of the reference folders, counts only: nothing real is shown."""
    if report.participants:
        table = Table(title="Generated fMRI dummy data")
        for column in ("Subject ID", "Scenario", "DICOM sessions", "Logs"):
            table.add_column(column)
        for r in report.participants:
            table.add_row(r.subject_id, r.scenario, str(r.sessions), str(r.logs))
        console.print(table)
    if report.reference_sessions or report.reference_logs:
        console.print(
            f"Dummy twins of the reference folders: {report.reference_sessions} session folder(s), "
            f"{report.reference_logs} log(s); {report.placeholders} word(s) replaced."
        )
    for count, what in (
        (report.no_tabs, "have no tabs: another delimiter?"),
        (report.unreadable, "couldn't be read (online only on OneDrive?)"),
        (report.unlike, "are read differently from their originals"),
    ):
        if count:
            console.print(f"{count} log(s) {what}", style="yellow")
    for name in (DICOM_DIR, BEHAV_DIR, DUMMY_DATA_LOG_FILENAME):
        if (path := output_folder / name).exists():
            console.print(str(path), markup=False, highlight=False)


def convert(output_folder: Path, bids_folder: Path) -> None:
    bids_folder.mkdir(parents=True, exist_ok=True)
    behav = output_folder / BEHAV_DIR
    with console.status("Converting to BIDS..."):
        report = ConvertJob(
            (output_folder / DICOM_DIR).resolve(),
            bids_folder.resolve(),
            behav.resolve() if behav.is_dir() else None,
            False,
            lambda *_step: None,
        ).run()
    if isinstance(report, str):
        raise click.ClickException(report)
    for what, count, color in report_rows(report):
        if count:
            console.print(f"{what}: {count}", style=color or None, highlight=False)
    for line in report_lines(report):
        logging.getLogger(__name__).info(line)
    console.print(str(bids_folder), markup=False, highlight=False)


if __name__ == "__main__":
    main()  # pyright: ignore[reportCallIssue] -- click supplies the arguments
