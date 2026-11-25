"""fmri_mriqc: run MRIQC on a BIDS folder and list its metrics with the outliers, as the Scan
quality tab does."""

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from fmri_local_process.app import mriqc as am
from fmri_local_process.cli.folders import find_folders
from fmri_local_process.processing import mriqc as mq
from fmri_local_process.processing.cpac_docker import docker_resources

console = Console()
FOLDER = click.Path(exists=True, file_okay=False, path_type=Path)
EPILOG = """
Examples:

\b
  fmri_mriqc                     run the subjects that aren't done (folders: fmri_folders)
  fmri_mriqc --results           only list the metrics
  fmri_mriqc --rate sub-P001_ses-1_task-Rest_bold 1 --comment "spikes"
  fmri_mriqc D:\\other_bids D:\\other_mriqc
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
@click.argument("bids_root", required=False, type=FOLDER)
@click.argument("output_root", required=False, type=click.Path(file_okay=False, path_type=Path))
@click.option("--subject", "only", multiple=True, help="Only this subject (repeat for more).")
@click.option("--redo", is_flag=True, help="Also subjects that are done.")
@click.option("--cpus", type=click.IntRange(1), help="Default: what Docker allows.")
@click.option("--mem-gb", type=click.FloatRange(mq.MIN_MEM_GB), help="Default: what Docker allows.")
@click.option("--results", is_flag=True, help="Only list the metrics; run nothing.")
@click.option(
    "--rate", type=(str, click.Choice(["0", "1", "2", "3"])), help="Rate a run (0: none)."
)
@click.option("--comment", default="", help="With --rate.")
def main(
    bids_root: Path | None,
    output_root: Path | None,
    only: tuple[str, ...],
    redo: bool,
    cpus: int | None,
    mem_gb: float | None,
    results: bool,
    rate: tuple[str, str] | None,
    comment: str,
) -> None:
    """Run MRIQC on BIDS_ROOT (in Docker) into OUTPUT_ROOT, then list each run's metrics.

    Values that are outliers among the same kind of scan are marked. Ratings: 1 bad (discard),
    2 unsure, 3 keep.
    """
    roots, _more, _remembered = find_folders({"bids": bids_root, "mriqc": output_root})
    bids_root, output_root = roots["bids"], roots["mriqc"]
    if rate is not None:
        name, value = rate
        mq.save_rating(output_root, name, int(value) or None, comment)
        click.echo(f"{name}: {mq.RATINGS.get(int(value), 'no rating')}")
        return
    failed = not results and not run_batch(bids_root, output_root, only, redo, cpus, mem_gb)
    print_results(bids_root, output_root)
    if failed:
        raise SystemExit(1)


def run_batch(
    bids_root: Path,
    output_root: Path,
    only: tuple[str, ...],
    redo: bool,
    cpus: int | None,
    mem_gb: float | None,
) -> bool:
    """False when something failed."""
    selection = mq.EVERYTHING
    subjects, states = am.subjects_and_states(bids_root, output_root, selection)
    wanted = [s.removeprefix("sub-") for s in only] or subjects
    if unknown := [s for s in wanted if s not in subjects]:
        raise click.ClickException(f"No such subject: {', '.join(unknown)}")
    to_run = [s for s in wanted if redo or states.get(s) != mq.DONE]
    if not to_run:
        click.echo("Every subject is done (--redo runs them again).")
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
        docker, bids_root, output_root, to_run, cpus or best_cpus, mem_gb or best_gb, selection,
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
