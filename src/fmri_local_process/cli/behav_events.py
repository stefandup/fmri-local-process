"""fmri_behav_events: convert one Presentation .log to a BIDS events.tsv (optionally vs R)."""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler

from fmri_local_process.processing.behav_compare import compare_with_r, render_r_events
from fmri_local_process.processing.behav_logs import (
    BEHAV_TASKS,
    log_subject_id,
    read_log_lines,
    task_for_log,
    write_events_tsv,
)
from fmri_local_process.processing.philani_bids import bids_stem

console = Console()
TASKS = {t.task: t for t in BEHAV_TASKS}


@click.command()
@click.version_option(package_name="fmri-local-process")
@click.argument("log_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--task",
    type=click.Choice(list(TASKS)),
    help="Task the log belongs to. Default: guessed from 'cogreappraisal'/'emolabeling' in "
    "the file or folder name.",
)
@click.option(
    "-o",
    "--out-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Where sub-<id>_task-<task>_events.tsv is written.",
)
@click.option(
    "--compare",
    is_flag=True,
    help="TEMPORARY: also run the original Rmd via Rscript and report differences.",
)
@click.option(
    "--rmd-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    envvar="PHILANI_RMD_DIR",
    help="philani_scripts/behavioural_processing_r_scripts (for --compare). Env: PHILANI_RMD_DIR.",
)
@click.option(
    "--rscript",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Rscript executable (for --compare). Default: Rscript on PATH.",
)
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def main(
    log_file: Path,
    task: str | None,
    out_dir: Path,
    compare: bool,
    rmd_dir: Path | None,
    rscript: Path | None,
    verbose: bool,
) -> None:
    """Convert one behavioural LOG_FILE to a BIDS events.tsv.

    Same code the conversion step (fmri_convert_to_bids --behav-logs) uses. Onset/duration
    are in seconds, onset measured from the first scanner pulse in the log (BIDS convention).
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )
    behav_task = TASKS[task] if task else task_for_log(log_file)
    if behav_task is None:
        raise click.UsageError(f"Can't tell the task from {log_file}; pass --task.")
    subject_id = log_subject_id(log_file)

    try:
        table = behav_task.extract(read_log_lines(log_file))
    except (ValueError, KeyError) as e:
        raise click.ClickException(f"{log_file.name}: {e!r}") from e
    out_dir.mkdir(parents=True, exist_ok=True)
    tsv = out_dir / f"{bids_stem(f'sub-{subject_id}', f'task-{behav_task.task}', 'events')}.tsv"
    write_events_tsv(table, tsv)
    console.print(f"Wrote {len(table.onset)} events to {tsv}")

    if not compare:
        return
    if rmd_dir is None:
        raise click.UsageError("--compare needs --rmd-dir (or PHILANI_RMD_DIR).")
    rscript = rscript or (Path(found) if (found := shutil.which("Rscript")) else None)
    if rscript is None:
        raise click.UsageError("--compare needs Rscript on PATH, or pass --rscript.")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            r_csv = render_r_events(rscript, rmd_dir, behav_task, log_file, subject_id, Path(tmp))
            diffs = compare_with_r(table, r_csv)
        except subprocess.CalledProcessError as e:
            raise click.ClickException(f"Rscript failed:\n{e.stderr}") from e
        except (OSError, ValueError, KeyError) as e:
            raise click.ClickException(f"R comparison failed: {e!r}") from e
    if diffs:
        for diff in diffs:
            console.print(f"[red]DIFF[/red] {diff}")
        raise SystemExit(1)
    console.print("[green]Python and R agree[/green] (R times converted to s, aligned at row 1)")


if __name__ == "__main__":
    main()  # pyright: ignore[reportCallIssue] -- click supplies the arguments
