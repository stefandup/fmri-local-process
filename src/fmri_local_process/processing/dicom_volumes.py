"""Missing volumes: does each BOLD run have every volume its DICOM series should have?

The checks dcm2niix makes while converting (its 'Missing images?' warnings), redone per run so
they can be looked at after conversion, next to the NIfTI's volumes:

* Instance numbers (0020,0013): the scanner numbers a series' images 1, 2, 3, ... A gap
  between the lowest and the highest means images are missing from the folder (lost in the
  export or the copy). dcm2niix: "Missing images? Expected N images, but instance number
  (0020,0013) ranges from a to b".
* Slice positions: every volume repeats the same slice positions, so images / positions is
  the number of complete volumes (a Siemens mosaic holds a whole volume, so 1 position).
  dcm2niix: "Expected N volumes but found spatial position repeats M times".
* The protocol's planned volumes: NumberOfTemporalPositions (0020,0105) where the scanner
  writes it, else the Siemens protocol in the CSA series header (lRepetitions + 1). Fewer
  acquired than planned means the scan was stopped early: nothing is lost from the folder.

Only these tags are read from each file, never the patient fields.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fmri_local_process.processing.crosscheck import DataFile
from fmri_local_process.processing.sequence_params import dicom_series, series_of_run

SIEMENS_PROTOCOL = (0x0029, 0x1020)  # CSA series header: the protocol as ASCCONV text
REPETITIONS = re.compile(rb"lRepetitions\s*=\s*(\d+)")
POSITION_DECIMALS = 2  # mm: positions this close are the same slice
PER_FILE_TAGS = ["InstanceNumber", "ImagePositionPatient", "NumberOfFrames"]
FIRST_FILE_TAGS = [*PER_FILE_TAGS, "NumberOfTemporalPositions", SIEMENS_PROTOCOL]

# Results, worst first
MISSING, DIFFERS, INCOMPLETE, STOPPED, OK, NOT_CHECKED = (
    "missing images",
    "NIfTI differs",
    "incomplete volume",
    "stopped early",
    "ok",
    "not checked",
)


@dataclass(frozen=True)
class SeriesVolumes:
    """What a DICOM series folder holds."""

    images: int  # readable DICOM images
    first_instance: int | None  # lowest / highest InstanceNumber, None when none had one
    last_instance: int | None
    positions: int  # distinct slice positions per volume (1 for a mosaic)
    planned: int | None = None  # volumes the protocol asked for, None when not recorded
    multiframe: bool = False  # an enhanced (multi-frame) DICOM: one file holds many images

    @property
    def expected_images(self) -> int | None:
        """The images the instance numbers say there should be."""
        if self.first_instance is None or self.last_instance is None:
            return None
        return self.last_instance - self.first_instance + 1

    @property
    def missing_images(self) -> int:
        expected = self.expected_images
        return max(0, expected - self.images) if expected is not None else 0

    @property
    def volumes(self) -> int:
        """Complete volumes."""
        return self.images // max(self.positions, 1)

    @property
    def leftover_images(self) -> int:
        """Images of a last, incomplete volume."""
        return self.images % max(self.positions, 1)


def planned_volumes(ds: object) -> int | None:
    """NumberOfTemporalPositions, else Siemens' lRepetitions + 1; None when neither."""
    temporal = getattr(ds, "NumberOfTemporalPositions", None)
    if temporal not in (None, ""):
        try:
            return int(str(temporal))
        except ValueError:
            pass
    try:
        protocol = ds[SIEMENS_PROTOCOL].value  # type: ignore[index]
    except (KeyError, TypeError):
        return None
    if isinstance(protocol, str):
        protocol = protocol.encode("latin-1", errors="replace")
    match = REPETITIONS.search(protocol) if isinstance(protocol, bytes) else None
    return int(match.group(1)) + 1 if match else None


def count_series(folder: Path) -> SeriesVolumes:
    """Read every file's instance number and slice position (headers only, a few tags)."""
    import pydicom  # here, so the rest of the toolbox works without it
    from pydicom.errors import InvalidDicomError

    instances: list[int] = []
    positions: set[tuple[float, ...]] = set()
    images, planned, multiframe = 0, None, False
    for path in sorted(f for f in folder.iterdir() if f.is_file()):
        tags = PER_FILE_TAGS if images else FIRST_FILE_TAGS
        try:
            ds = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=tags)
        except (InvalidDicomError, OSError, ValueError, AttributeError):
            continue
        if not images:
            planned = planned_volumes(ds)
        images += 1
        if int(getattr(ds, "NumberOfFrames", 1) or 1) > 1:
            multiframe = True
        number = getattr(ds, "InstanceNumber", None)
        if number not in (None, ""):
            instances.append(int(number))
        position = getattr(ds, "ImagePositionPatient", None)
        if position is not None:
            positions.add(tuple(round(float(v), POSITION_DECIMALS) for v in position))
    return SeriesVolumes(
        images,
        min(instances) if instances else None,
        max(instances) if instances else None,
        max(len(positions), 1),
        planned,
        multiframe,
    )


@dataclass(frozen=True)
class VolumeCheck:
    """One BOLD run against its DICOM series."""

    data: DataFile
    series: str = ""  # the series folder's name
    counts: SeriesVolumes | None = None
    note: str = ""  # why it wasn't checked

    @property
    def name(self) -> str:
        return self.data.path.name.removesuffix(".nii.gz")

    @property
    def nifti_volumes(self) -> int | None:
        """As converted: before a trim, which cut volumes on purpose."""
        return self.data.trimmed_from or self.data.volumes

    @property
    def result(self) -> str:
        counts = self.counts
        if counts is None or counts.multiframe:
            return NOT_CHECKED
        if counts.missing_images:
            return MISSING
        if self.nifti_volumes is not None and self.nifti_volumes != counts.volumes:
            return DIFFERS
        if counts.leftover_images:
            return INCOMPLETE
        if counts.planned is not None and counts.volumes < counts.planned:
            return STOPPED
        return OK

    @property
    def explanation(self) -> str:
        """The result in a sentence."""
        counts, result = self.counts, self.result
        if counts is None:
            return self.note or "Not checked."
        if counts.multiframe:
            return "Enhanced (multi-frame) DICOM: one file holds many images; not counted here."
        if result == MISSING:
            return (
                f"{counts.missing_images} image(s) missing from the DICOM folder: instance "
                f"numbers run {counts.first_instance}-{counts.last_instance} "
                f"({counts.expected_images} images) but {counts.images} are there. Lost in "
                "the export or the copy: export the series from the scanner/PACS again."
            )
        if result == DIFFERS:
            return (
                f"The NIfTI has {self.nifti_volumes} volumes, the DICOMs {counts.volumes} "
                "complete ones: convert this run again."
            )
        if result == INCOMPLETE:
            return (
                f"The last volume is incomplete: {counts.leftover_images} of its "
                f"{counts.positions} slices (scan stopped mid-volume)."
            )
        if result == STOPPED:
            return (
                f"Stopped early: {counts.volumes} of the {counts.planned} volumes the protocol "
                "planned. Nothing is missing from the folder; usually the scan was stopped by "
                "hand."
            )
        return "Every image is there and the NIfTI has every volume."


def bold_runs(data_files: list[DataFile]) -> list[DataFile]:
    """The 4D BOLD runs, staged ones too (they are runs to choose from)."""
    return sorted(
        (d for d in data_files if d.datatype == "func" and d.volumes and d.volumes > 1),
        key=lambda d: d.path.name,
    )


def check_runs(
    bids_root: Path,
    dicom_root: Path,
    runs: list[DataFile],
    progress: Callable[[int, int], None] | None = None,
    stopped: Callable[[], bool] = lambda: False,
) -> list[VolumeCheck]:
    """Each run against its DICOM series (found as the converter found it); stops between
    runs once stopped() is true."""
    stems_by_subject: dict[str, dict[str, list[Path]]] = {}
    checks = []
    for number, data in enumerate(runs, start=1):
        if stopped():
            break
        if data.subject not in stems_by_subject:
            stems_by_subject[data.subject] = dicom_series(dicom_root, bids_root, data.subject)
        series, _tags, why_not = series_of_run(data.path, stems_by_subject[data.subject])
        if series is None:
            checks.append(VolumeCheck(data, note=why_not))
        else:
            checks.append(VolumeCheck(data, series.name, count_series(series)))
        if progress is not None:
            progress(number, len(runs))
    return checks
