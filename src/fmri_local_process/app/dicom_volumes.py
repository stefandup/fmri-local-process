"""Missing volumes: each BOLD run's DICOM images counted next to the NIfTI's volumes (the
counting is processing/dicom_volumes.py)."""

from collections.abc import Callable
from pathlib import Path

from fmri_local_process.app.colors import ERROR_COLOR, OK_COLOR, UNKNOWN_COLOR, WARNING_COLOR
from fmri_local_process.processing import dicom_volumes as dv
from fmri_local_process.processing.crosscheck import DataFile

RESULT_COLOR = {
    dv.MISSING: ERROR_COLOR,
    dv.DIFFERS: ERROR_COLOR,
    dv.INCOMPLETE: WARNING_COLOR,
    dv.STOPPED: WARNING_COLOR,
    dv.OK: OK_COLOR,
    dv.NOT_CHECKED: UNKNOWN_COLOR,
}
PROBLEMS = (dv.MISSING, dv.DIFFERS, dv.INCOMPLETE)
# (header, tooltip)
COLUMNS = (
    ("Run", "The BOLD run in the BIDS folder."),
    ("DICOM series", "The DICOM series folder it was converted from."),
    (
        "DICOM images",
        "How many DICOM image files the series folder holds. A Siemens mosaic file holds a "
        "whole volume; otherwise each file is one slice.",
    ),
    (
        "Instance numbers",
        "The range of the images' instance numbers (DICOM tag 0020,0013). The scanner numbers "
        "a series' images 1, 2, 3, ... with no gaps, so the range says how many images there "
        "should be. This is how dcm2niix spots missing images.",
    ),
    (
        "Missing images",
        "Images the instance numbers say should be there, but aren't: (last - first + 1) - "
        "images found. Not 0 means files were lost in the export or the copy: dcm2niix's "
        "'Missing images? Expected N images, but instance number ranges from a to b'.",
    ),
    (
        "Volumes (DICOM)",
        "Complete volumes in the DICOM folder: images / slice positions per volume (dcm2niix "
        "counts how often the first slice position repeats).",
    ),
    (
        "Planned (protocol)",
        "The volumes the scan protocol asked for (NumberOfTemporalPositions, or the Siemens "
        "protocol's repetitions + 1). Fewer acquired means the scan was stopped early, which "
        "isn't missing data. Blank when the scanner doesn't record it.",
    ),
    (
        "Volumes (NIfTI)",
        "The volumes dcm2niix wrote into the BIDS NIfTI (before any trim on this tab: "
        "trimming cuts volumes on purpose).",
    ),
    ("Result", "What the numbers mean: hover a run's result for the full sentence."),
)
HELP_TEXT = (
    "dcm2niix warns while converting when DICOM images seem to be missing. This redoes its "
    "checks per BOLD run, so they can be looked at afterwards:<br><br>"
    "<b>Missing images</b> (red): the scanner numbers a series' images without gaps "
    "(instance numbers 1, 2, 3, ...). A gap means files were lost between the scanner and "
    "this folder, so volumes (or slices of them) are missing. Export the series again.<br>"
    "<b>NIfTI differs</b> (red): the NIfTI doesn't have the volumes the DICOM folder holds: "
    "convert the run again.<br>"
    "<b>Incomplete volume</b> (orange): the last volume has only some of its slices: the scan "
    "was stopped mid-volume; dcm2niix leaves it out.<br>"
    "<b>Stopped early</b> (orange): every image is there, but fewer volumes than the "
    "protocol planned: the scan was stopped before its end. Nothing is lost; check the task "
    "still fits (Expected volumes above).<br><br>"
    "Reads a few header tags of every DICOM file (never the patient fields): a minute or so "
    "per few hundred runs."
)


class CheckJob:
    def __init__(
        self,
        bids_root: Path,
        dicom_root: Path,
        runs: list[DataFile],
        progress: Callable[[int, int], None],
    ) -> None:
        self.bids_root, self.dicom_root, self.runs = bids_root, dicom_root, runs
        self.progress = progress
        self.stop_requested = False

    def stop(self) -> None:
        """Safe from any thread: ends after the run being read."""
        self.stop_requested = True

    def run(self) -> tuple[list[dv.VolumeCheck], str]:
        """(the checks, why they couldn't be made)."""
        try:
            checks = dv.check_runs(
                self.bids_root,
                self.dicom_root,
                self.runs,
                self.progress,
                lambda: self.stop_requested,
            )
        except (OSError, ImportError) as e:
            return [], str(e)
        return checks, ""


def status_text(dicom_root: Path | None, runs: list[DataFile], checks: list[dv.VolumeCheck]) -> str:
    if dicom_root is None:
        return "Choose the DICOM sessions folder (Setup tab) to check."
    if not runs:
        return "No BOLD runs to check."
    if not checks:
        return f"{len(runs)} BOLD run(s) not checked yet."
    problems = sum(1 for c in checks if c.result in PROBLEMS)
    stopped = sum(1 for c in checks if c.result == dv.STOPPED)
    return (
        f"{len(checks)} run(s) checked: {problems} with missing or unconverted "
        f"volumes, {stopped} stopped early."
    )


def check_cells(check: dv.VolumeCheck) -> list[str]:
    """One run's row (COLUMNS)."""
    counts = check.counts
    cells = [check.name, check.series or "-"]
    if counts is None:
        cells += ["", "", "", "", ""]
    else:
        instances = (
            f"{counts.first_instance}-{counts.last_instance}"
            if counts.first_instance is not None
            else "-"
        )
        cells += [
            str(counts.images),
            instances,
            str(counts.missing_images),
            str(counts.volumes),
            "" if counts.planned is None else str(counts.planned),
        ]
    return [*cells, str(check.nifti_volumes or ""), check.result]
