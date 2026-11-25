"""fmri_mriqc: run MRIQC on a BIDS folder and list its metrics with the outliers, as the Scan
quality tab does."""

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from fmri_local_process.app import mriqc as am
from fmri_local_process.processing import mriqc as mq
from fmri_local_process.processing.cpac_docker import docker_resources

console = Console()
FOLDER = click.Path(exists=True, file_okay=False, path_type=Path)
EPILOG = """
Examples:

\b
  fmri_mriqc path/to/bids path/to/mriqc                     run the subjects that aren't done
  fmri_mriqc path/to/bids path/to/mriqc --subject_id P001   only this one
  fmri_mriqc path/to/bids path/to/mriqc --results           only list the metrics

Rate the runs in the toolbox (fmri_toolbox, or fmri_tui without a display): Scan quality.
"""


class Printer:
    """MriqcListener: progress as lines."""

    def __init__(self) -> None:
        self.failed = False

    def on_message(self, text: str, is_error: bool, /) -> None:
        self.failed |= is_error
        console.print(text, style="red" if is_error else None, markup=False, highlight=False)

    def on_subject_started(self, subject: str, /) -> None: ...

    def on_step(self, subject: str, steps: int, node: str, /) -> None: ...

    def on_subject_done(self, subject: str, failure: str, /) -> None: ...

    def on_stage(self, text: str, /) -> None: ...

    def on_all_done(self, summary: str, /) -> None:
        console.print(summary, markup=False)


def print_results(bids_root: Path, output_root: Path) -> None:
    ratings = mq.read_ratings(output_root)
    for modality in mq.MODALITIES:
        runs, unreadable = mq.collect_iqms(output_root, modality, bids_root)
        flags = mq.flag_outliers(runs)
        if runs:
            metrics = [m.label for m in mq.METRICS[modality]]
            table = Table("Run", "Rating", "Flags", *metrics, title=am.MODALITY_TEXT[modality])
            for run in runs:
                flagged = flags.get(run.name, set())
                cells = [
                    f"[black on red]{text}[/]" if key in flagged else text
                    for key, text in am.metric_cells(run, modality)
                ]
                rating = am.rating_text(ratings.get(run.name))
                table.add_row(run.name, rating, am.flags_text(run, flagged), *cells)
            console.print(table)
        for name in unreadable:
            console.print(f"Couldn't read {name}", style="yellow", markup=False)


@click.command(epilog=EPILOG)
@click.version_option(package_name="fmri-local-process")
@click.argument("bids_root", type=FOLDER, required=True)
@click.argument("output_folder", type=click.Path(file_okay=False, path_type=Path), required=True)
@click.option("--subject_id", default="", help="Only this subject (no 'sub-').")
@click.option("--rerun", is_flag=True, help="Redo subjects whose output is complete.")
@click.option("--n-cpus", type=click.IntRange(1), help="Default: what Docker allows.")
@click.option("--mem-gb", type=click.FloatRange(mq.MIN_MEM_GB), help="Default: what Docker allows.")
@click.option("--results", is_flag=True, help="Only list the metrics; run nothing.")
def main(
    bids_root: Path,
    output_folder: Path,
    subject_id: str,
    rerun: bool,
    n_cpus: int | None,
    mem_gb: float | None,
    results: bool,
) -> None:
    """Run MRIQC on BIDS_ROOT (in Docker) into OUTPUT_FOLDER, then list each run's metrics.

    Values that are outliers among the same kind of scan are marked.
    """
    failed = not results and not run_batch(
        bids_root, output_folder, subject_id, rerun, n_cpus, mem_gb
    )
    print_results(bids_root, output_folder)
    if failed:
        raise SystemExit(1)


def run_batch(
    bids_root: Path,
    output_root: Path,
    subject_id: str,
    rerun: bool,
    n_cpus: int | None,
    mem_gb: float | None,
) -> bool:
    """False when something failed."""
    selection = mq.EVERYTHING
    subjects, states = am.subjects_and_states(bids_root, output_root, selection)
    wanted = [subject_id.removeprefix("sub-")] if subject_id else subjects
    if unknown := [s for s in wanted if s not in subjects]:
        raise click.ClickException(f"No such subject: {', '.join(unknown)}")
    to_run = [s for s in wanted if rerun or states.get(s) != mq.DONE]
    if not to_run:
        click.echo("Every subject is done (--rerun runs them again).")
        return True
    state, message, docker = mq.docker_status()
    if docker is None or state != mq.READY:
        hint = f" Download it with: docker pull {mq.MRIQC_IMAGE}"
        raise click.ClickException(message + (hint if state == mq.IMAGE_MISSING else ""))
    docker_gb, docker_cpus = docker_resources(docker)
    best_cpus, best_gb = mq.suggest_resources(docker_cpus, docker_gb)
    output_root.mkdir(parents=True, exist_ok=True)
    printer = Printer()
    job = am.MriqcJob(
        docker, bids_root, output_root, to_run, n_cpus or best_cpus, mem_gb or best_gb, selection,
        printer,
    )  # fmt: skip
    try:
        job.run()
    except KeyboardInterrupt:
        job.stop_and_wait()
        raise
    return not printer.failed


if __name__ == "__main__":
    main()  # pyright: ignore[reportCallIssue] -- click supplies the arguments
