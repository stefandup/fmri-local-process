"""fmri_preprocess: run C-PAC's standard 'preproc' pipeline on a BIDS dataset, via Docker.

Same shape as the other batch CLIs: input -> output folder, progress bar, --subject_id
filter, skip what's done unless --rerun, failures logged and skipped, summary at the end.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from fmri_local_process.processing.cpac_docker import (
    CPAC_IMAGE,
    MIN_MEM_GB,
    DockerUnavailableError,
    bids_subjects,
    docker_memory_gb,
    find_docker,
    finished_node,
    host_memory_gb,
    image_present,
    is_subject_done,
    memory_problem,
    preprocess_subject,
    pull_image,
)

log = logging.getLogger(__name__)
console = Console()

EXAMPLES_EPILOG = """
Examples:

\b
  Preprocess every subject (Docker Desktop must be running; first run pulls ~10+ GB):
  fmri_preprocess D:\\philani_bids D:\\philani_cpac --n-cpus 4 --mem-gb 8

\b
  Quick test: one subject, only its resting-state runs:
  fmri_preprocess D:\\philani_bids D:\\philani_cpac --subject_id P001 --task Rest --n-cpus 4

\b
  One subject, redoing it even if output already exists:
  fmri_preprocess D:\\philani_bids D:\\philani_cpac --subject_id P001 --rerun
"""


@dataclass
class PreprocSummary:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (subject, why)


def print_summary(summary: PreprocSummary, output_root: Path) -> None:
    table = Table(title="C-PAC preprocessing")
    table.add_column("Result")
    table.add_column("Count", justify="right")
    table.add_row("preprocessed", str(len(summary.done)))
    table.add_row("skipped (done, use --rerun)", str(len(summary.skipped)))
    table.add_row("[red]failed", f"[red]{len(summary.failed)}")
    console.print(table)
    for subject, why in summary.failed:
        console.print(f"[red]FAILED[/red] sub-{subject}: {why}")
    if summary.failed:
        console.print(f"C-PAC crash files and logs: {output_root / 'log'}")


@click.command(epilog=EXAMPLES_EPILOG)
@click.version_option(package_name="fmri-local-process")
@click.argument("bids_root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("output_folder", type=click.Path(file_okay=False, path_type=Path))
@click.option("--subject_id", default="", help="Only preprocess this subject (no 'sub-').")
@click.option(
    "--task",
    help="Only preprocess this task's BOLD runs (the BIDS task label, e.g. Rest, Matching, "
    "Reappraisal). Default: every BOLD run.",
)
@click.option("--rerun", is_flag=True, help="Redo subjects whose output already exists.")
@click.option("--n-cpus", default=1, show_default=True, help="CPU cores C-PAC may use per subject.")
@click.option(
    "--mem-gb",
    default=MIN_MEM_GB,
    type=click.FloatRange(min=MIN_MEM_GB),
    show_default=True,
    help="RAM (GB) C-PAC may use per subject (C-PAC won't start below "
    f"{MIN_MEM_GB:g}). Docker Desktop's own limit must allow it.",
)
@click.option("--image", default=CPAC_IMAGE, show_default=True, help="C-PAC Docker image to run.")
@click.option("--verbose", "-v", is_flag=True, help="Debug logging.")
def main(
    bids_root: Path,
    output_folder: Path,
    subject_id: str,
    task: str | None,
    rerun: bool,
    n_cpus: int,
    mem_gb: float,
    image: str,
    verbose: bool,
) -> None:
    """Preprocess BIDS_ROOT with C-PAC's standard 'preproc' pipeline into OUTPUT_FOLDER.

    \b
    C-PAC 'preproc' (default pipeline minus derivatives) runs, per BOLD run:
      anat: BET brain extraction, FSL-FAST segmentation, ANTs registration to MNI152 2 mm
      func: slice-timing correction, 3dvolreg motion correction, distortion correction
            (if fieldmaps exist), AFNI masking, BBR coregistration to T1w, warp to MNI,
            nuisance regression (24 motion, 5 aCompCor, CSF, poly-2, 0.01-0.1 Hz
            bandpass), 4 mm smoothing, z-scoring

    Each subject runs in its own container, with only sub-<id>/ mounted, so
    crosscheck_pending/ is never used as input. Output: OUTPUT_FOLDER/output/
    pipeline_cpac_preproc/, C-PAC logs in OUTPUT_FOLDER/log/, and the console output of
    each run in OUTPUT_FOLDER/log/fmri_preprocess_sub-<id>.txt.

    A subject is skipped when every session already has a preprocessed BOLD (of --task,
    if given) -- pass --rerun to redo it. Exit code 1 if any subject failed.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )
    try:
        docker = find_docker()
    except DockerUnavailableError as e:
        raise click.ClickException(str(e)) from e
    problem = memory_problem(mem_gb, docker_memory_gb(docker), host_memory_gb())
    if problem is not None:
        raise click.ClickException(problem)
    if not (bids_root / "dataset_description.json").is_file():
        raise click.UsageError(f"{bids_root} has no dataset_description.json; not a BIDS root.")

    subjects = bids_subjects(bids_root, task)
    if subject_id:
        subjects = [s for s in subjects if s == subject_id]
    if not subjects:
        what = f"task-{task} BOLD runs" if task else "BOLD runs"
        raise click.UsageError(f"No subjects with {what} found in {bids_root}")

    if not image_present(docker, image):
        console.print(
            f"[yellow]C-PAC image {image} is not downloaded yet: pulling it now "
            "(~15 GB, one-time; can take 30+ min). Docker's layer progress follows.[/yellow]"
        )
        if pull_image(docker, image) != 0:
            raise click.ClickException(f"docker pull {image} failed; see the output above.")

    summary = PreprocSummary()
    output_folder.mkdir(parents=True, exist_ok=True)
    columns = (
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    )
    with Progress(*columns, console=console) as progress:
        bar = progress.add_task("Subjects", total=len(subjects))
        for subject in subjects:
            if not rerun and is_subject_done(bids_root, output_folder, subject, task):
                summary.skipped.append(subject)
                progress.advance(bar)
                continue
            # C-PAC never states how many steps a run has, so: steps done + last step, no %.
            steps = progress.add_task(f"  sub-{subject}: starting container", total=None)

            def show_step(line: str, subject: str = subject, steps: TaskID = steps) -> None:
                node = finished_node(line)
                if node is not None:
                    progress.advance(steps)
                    progress.update(steps, description=f"  sub-{subject}: {node}")

            failure = preprocess_subject(
                docker, bids_root, output_folder, subject, image, n_cpus, mem_gb, task, show_step
            )
            progress.remove_task(steps)
            if failure is not None:
                summary.failed.append((subject, failure))
            else:
                summary.done.append(subject)
            progress.advance(bar)

    print_summary(summary, output_folder)
    if summary.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()  # pyright: ignore[reportCallIssue] -- click supplies the arguments
