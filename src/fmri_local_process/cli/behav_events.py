"""fmri_behav_events: batch-convert Presentation .log files to BIDS events.tsv (optionally vs R).

Thin loop around processing/behav_logs.py -- the same code `fmri_convert_to_bids
--behav-logs` uses. Modelled on the vr_lab_toolbox batch CLIs: input -> output folder,
progress bar, --subject_id filter, skip what's done unless --rerun, failures logged and
skipped, summary at the end.
"""

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress
from rich.table import Table

from fmri_local_process.processing.behav_compare import compare_with_r, render_r_events
from fmri_local_process.processing.behav_logs import (
    BEHAV_TASKS,
    LogTarget,
    find_logs,
    log_subject_id,
    plan_log_targets,
    read_log_lines,
    write_events_tsv,
)

log = logging.getLogger(__name__)
console = Console()
TASKS = {t.task: t for t in BEHAV_TASKS}

EXAMPLES_EPILOG = """
Examples:

\b
  Every log under a folder (subfolders cogreappraisal/, emolabeling/ name the task):
  fmri_behav_events D:\\philani_behavioural_logs results\\events

\b
  One participant, redoing files that already exist, checked against the original R:
  fmri_behav_events D:\\philani_behavioural_logs results\\events --subject_id P001 --rerun --compare
"""


@dataclass
class BatchSummary:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (log name, why)
    r_agree: list[str] = field(default_factory=list)
    r_diff: list[tuple[str, list[str]]] = field(default_factory=list)


@dataclass(frozen=True)
class RCompare:
    rscript: Path
    rmd_dir: Path


def convert_target(
    target: LogTarget, output_folder: Path, compare: RCompare | None, summary: BatchSummary
) -> None:
    """One log -> output_folder/<stem>.tsv; failures go into the summary, not raised."""
    name = target.log_file.name
    try:
        table = target.task.extract(read_log_lines(target.log_file))
    except (OSError, ValueError, KeyError) as e:
        log.warning("Skipping %s: %r", name, e)
        summary.failed.append((name, repr(e)))
        return
    write_events_tsv(table, output_folder / f"{target.stem}.tsv")
    summary.written.append(target.stem)

    if compare is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        try:
            r_csv = render_r_events(
                compare.rscript,
                compare.rmd_dir,
                target.task,
                target.log_file,
                log_subject_id(target.log_file),
                Path(tmp),
            )
            diffs = compare_with_r(table, r_csv)
        except subprocess.CalledProcessError as e:
            summary.failed.append((name, f"Rscript failed: {e.stderr.strip()[-500:]}"))
            return
        except (OSError, ValueError, KeyError) as e:
            summary.failed.append((name, f"R comparison failed: {e!r}"))
            return
    if diffs:
        summary.r_diff.append((target.stem, diffs))
    else:
        summary.r_agree.append(target.stem)


def print_summary(summary: BatchSummary, compared: bool) -> None:
    table = Table(title="Behavioural events")
    table.add_column("Result")
    table.add_column("Count", justify="right")
    table.add_row("written", str(len(summary.written)))
    table.add_row("skipped (exists, use --rerun)", str(len(summary.skipped)))
    table.add_row("[red]failed", f"[red]{len(summary.failed)}")
    if compared:
        table.add_row("[green]R agrees", f"[green]{len(summary.r_agree)}")
        table.add_row("[red]R differs", f"[red]{len(summary.r_diff)}")
    console.print(table)
    for name, why in summary.failed:
        console.print(f"[red]FAILED[/red] {name}: {why}")
    for stem, diffs in summary.r_diff:
        for diff in diffs:
            console.print(f"[red]DIFF[/red] {stem} {diff}")


@click.command(epilog=EXAMPLES_EPILOG)
@click.version_option(package_name="fmri-local-process")
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.argument("output_folder", type=click.Path(file_okay=False, path_type=Path))
@click.option("--subject_id", default="", help="Only convert this participant's logs.")
@click.option(
    "--task",
    type=click.Choice(list(TASKS)),
    help="Treat every log as this task. Default: from 'cogreappraisal'/'emolabeling' in "
    "the log's path below INPUT_PATH (file name or any folder name).",
)
@click.option("--rerun", is_flag=True, help="Overwrite events.tsv files that already exist.")
@click.option(
    "--compare",
    is_flag=True,
    help="TEMPORARY: also run the original Rmd via Rscript on each log and report differences.",
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
@click.option("--verbose", "-v", is_flag=True, help="Debug logging.")
def main(
    input_path: Path,
    output_folder: Path,
    subject_id: str,
    task: str | None,
    rerun: bool,
    compare: bool,
    rmd_dir: Path | None,
    rscript: Path | None,
    verbose: bool,
) -> None:
    """Convert behavioural Presentation logs to BIDS events.tsv files.

    INPUT_PATH is one .log file or a folder searched recursively for *.log. Each log is
    written to OUTPUT_FOLDER as sub-<id>_task-<task>_events.tsv (run-<n> added when a
    participant has several logs for one task). Onset/duration are in seconds, onset
    measured from the first scanner pulse in the log (BIDS convention).

    By default, skips logs whose events.tsv already exists -- pass --rerun to redo them.
    Exit code 1 if any log failed or (with --compare) differed from R.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )
    r_compare = None
    if compare:
        if rmd_dir is None:
            raise click.UsageError("--compare needs --rmd-dir (or PHILANI_RMD_DIR).")
        rscript = rscript or (Path(found) if (found := shutil.which("Rscript")) else None)
        if rscript is None:
            raise click.UsageError("--compare needs Rscript on PATH, or pass --rscript.")
        r_compare = RCompare(rscript, rmd_dir)

    logs = find_logs(input_path)
    if subject_id:
        logs = [p for p in logs if log_subject_id(p) == subject_id]
    root = input_path if input_path.is_dir() else None
    targets, unknown = plan_log_targets(logs, TASKS[task] if task else None, root)
    if not targets and not unknown:
        raise click.UsageError(f"No .log files found in {input_path}")

    summary = BatchSummary()
    summary.failed += [(p.name, "can't tell the task; pass --task") for p in unknown]
    output_folder.mkdir(parents=True, exist_ok=True)
    with Progress(console=console) as progress:
        bar = progress.add_task("Converting logs", total=len(targets))
        for target in targets:
            progress.update(bar, description=f"Converting {target.log_file.name}", advance=1)
            if not rerun and (output_folder / f"{target.stem}.tsv").exists():
                summary.skipped.append(target.stem)
                continue
            convert_target(target, output_folder, r_compare, summary)

    print_summary(summary, compared=compare)
    if summary.failed or summary.r_diff:
        raise SystemExit(1)


if __name__ == "__main__":
    main()  # pyright: ignore[reportCallIssue] -- click supplies the arguments
