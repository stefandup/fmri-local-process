"""Scan (sequence) parameters of a subject's runs, side by side, for the Sequence overview tab.

Three sources, best first:

* the NIfTI header: matrix, voxel size, volumes, TR as stored, orientation, data type;
* the BIDS sidecar (.json) that dcm2niix wrote from the DICOMs at conversion;
* the raw DICOM series the run came from (on request): fills in what the sidecar lacks
  (a sidecar lost or written by an older dcm2niix), and adds the DICOM fields dcm2niix
  doesn't copy (acquisition matrix, phase FOV, averages, echo train length, ...).

Only the whitelisted tags in DICOM_ONLY and PARAMS are read from a DICOM file: never the
patient fields. The DICOM series of a run is found as the converter found it (session
folders, scan rules, crosscheck corrections); when it can't be told apart from another
series, it is left out rather than guessed.
"""

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import nibabel as nib
import numpy as np
from nibabel.filebasedimages import ImageFileError

from fmri_local_process.processing.cpac_outputs import bids_entities
from fmri_local_process.processing.philani_bids import (
    bids_stem,
    find_sessions,
    match_series,
    number_sessions,
    read_dicom_corrections,
    read_dicom_date_corrections,
    read_dicom_session_corrections,
    read_excluded_sessions,
    read_scan_rules,
)

log = logging.getLogger(__name__)

HEADER, SIDECAR, DICOM = "hdr", "json", "dcm"
SOURCE_MARKS = {HEADER: "ʰ", SIDECAR: "", DICOM: "ᵈ"}
SOURCE_LEGEND = "ʰ from the NIfTI header, ᵈ from the raw DICOM, unmarked from the BIDS sidecar"
DICOM_FILES_TRIED = 5  # files in a series folder tried before giving up on it
MAX_CELL = 34


@dataclass(frozen=True)
class Param:
    """One row: where its value comes from, and whether runs should agree on it."""

    label: str
    group: str
    sidecar: str | None = None
    dicom: str | None = None  # pydicom keyword
    dicom_scale: float = 1.0  # DICOM ms -> sidecar s
    compare: bool = True  # False for fields that always differ (series number, time)


PARAMS = (
    Param("Manufacturer", "Scanner", "Manufacturer", "Manufacturer"),
    Param("Model", "Scanner", "ManufacturersModelName", "ManufacturerModelName"),
    Param("Field strength (T)", "Scanner", "MagneticFieldStrength", "MagneticFieldStrength"),
    Param("Software", "Scanner", "SoftwareVersions", "SoftwareVersions"),
    Param("Receive coil", "Scanner", "ReceiveCoilName", "ReceiveCoilName"),
    Param("Coil elements", "Scanner", "CoilString"),
    Param("Protocol", "Sequence", "ProtocolName", "ProtocolName"),
    Param("Series description", "Sequence", "SeriesDescription", "SeriesDescription"),
    Param("Series number", "Sequence", "SeriesNumber", "SeriesNumber", compare=False),
    Param("Acquisition time", "Sequence", "AcquisitionTime", "AcquisitionTime", compare=False),
    Param("Sequence name", "Sequence", "SequenceName", "SequenceName"),
    Param("Pulse sequence", "Sequence", "PulseSequenceDetails"),
    Param("Scanning sequence", "Sequence", "ScanningSequence", "ScanningSequence"),
    Param("Sequence variant", "Sequence", "SequenceVariant", "SequenceVariant"),
    Param("Scan options", "Sequence", "ScanOptions", "ScanOptions"),
    Param("2D / 3D", "Sequence", "MRAcquisitionType", "MRAcquisitionType"),
    Param("TR (s)", "Timing", "RepetitionTime", "RepetitionTime", 0.001),
    Param("TE (s)", "Timing", "EchoTime", "EchoTime", 0.001),
    Param("Flip angle (deg)", "Timing", "FlipAngle", "FlipAngle"),
    Param("Slice timing", "Timing", "SliceTiming"),
    Param("Slice thickness (mm)", "Geometry", "SliceThickness", "SliceThickness"),
    Param("Slice spacing (mm)", "Geometry", "SpacingBetweenSlices", "SpacingBetweenSlices"),
    Param("Phase encoding", "Geometry", "PhaseEncodingDirection"),
    Param("Phase encoding (DICOM)", "Geometry", None, "InPlanePhaseEncodingDirection"),
    Param("Acq. matrix PE", "Geometry", "AcquisitionMatrixPE"),
    Param("Recon matrix PE", "Geometry", "ReconMatrixPE"),
    Param("Multiband factor", "Acceleration", "MultibandAccelerationFactor"),
    Param("In-plane accel. (GRAPPA)", "Acceleration", "ParallelReductionFactorInPlane"),
    Param("Partial Fourier", "Acceleration", "PartialFourier"),
    Param("Pixel bandwidth (Hz/px)", "Readout", "PixelBandwidth", "PixelBandwidth"),
    Param("Echo spacing (s)", "Readout", "EffectiveEchoSpacing"),
    Param("Total readout (s)", "Readout", "TotalReadoutTime"),
    Param("SAR (W/kg)", "Safety", "SAR", "SAR", compare=False),
    Param("dcm2niix version", "Conversion", "ConversionSoftwareVersion"),
)
# DICOM fields dcm2niix doesn't put in the sidecar: shown once the DICOMs are read.
DICOM_ONLY = (
    Param("Acquisition matrix", "DICOM only", dicom="AcquisitionMatrix"),
    Param("Rows x columns", "DICOM only", dicom="Rows"),
    Param("Pixel spacing (mm)", "DICOM only", dicom="PixelSpacing"),
    Param("Phase FOV (%)", "DICOM only", dicom="PercentPhaseFieldOfView"),
    Param("Phase sampling (%)", "DICOM only", dicom="PercentSampling"),
    Param("Phase encoding steps", "DICOM only", dicom="NumberOfPhaseEncodingSteps"),
    Param("Echo train length", "DICOM only", dicom="EchoTrainLength"),
    Param("Averages", "DICOM only", dicom="NumberOfAverages"),
    Param("Imaging frequency (MHz)", "DICOM only", dicom="ImagingFrequency"),
    Param("Patient position", "DICOM only", dicom="PatientPosition"),
)
HEADER_PARAMS = (
    "Matrix (voxels)",
    "Voxel size (mm)",
    "Volumes",
    "TR in header (s)",
    "Duration (s)",
    "Orientation",
    "Data type",
)
HEADER_GROUP = "Image (NIfTI)"
DICOM_TAGS = tuple(
    sorted({p.dicom for p in (*PARAMS, *DICOM_ONLY) if p.dicom} | {"Columns", "SeriesNumber"})
)


@dataclass(frozen=True)
class Value:
    text: str
    source: str


@dataclass
class ScanRun:
    """One BIDS image of a subject, and the parameters found for it."""

    nifti: Path
    subject: str
    session: str
    scan: str  # "task-Checkerboard_bold", "T1w"
    run: str = ""
    values: dict[str, Value] = field(default_factory=dict)
    dicom_note: str = ""  # which series the DICOM fields came from, or why there are none

    @property
    def column(self) -> str:
        return f"ses-{self.session}" + (f" run-{self.run}" if self.run else "")

    @property
    def sidecar(self) -> Path:
        return self.nifti.with_name(self.nifti.name.removesuffix(".nii.gz") + ".json")


# ---- finding the runs ---------------------------------------------------------------------------


def scan_label(name: str) -> str:
    """'sub-A_ses-1_task-Rest_run-2_bold.nii.gz' -> 'task-Rest_bold'."""
    task = bids_entities(name).get("task")
    suffix = name.split(".", 1)[0].rsplit("_", 1)[-1]
    return f"task-{task}_{suffix}" if task else suffix


def find_runs(bids_root: Path, subject: str) -> list[ScanRun]:
    """Every anat and func NIfTI of sub-<subject>, in session order."""
    runs = []
    for nifti in sorted((bids_root / f"sub-{subject}").glob("ses-*/*/*.nii.gz")):
        if nifti.parent.name not in ("anat", "func"):
            continue
        entities = bids_entities(nifti.name)
        runs.append(
            ScanRun(
                nifti,
                subject,
                entities.get("ses", ""),
                scan_label(nifti.name),
                entities.get("run", ""),
            )
        )
    return sorted(runs, key=lambda r: (session_key(r.session), r.scan, r.run))


def session_key(label: str) -> tuple[int, str]:
    return (int(label), "") if label.isdigit() else (10**6, label)


def subjects(bids_root: Path) -> list[str]:
    return sorted(d.name.removeprefix("sub-") for d in bids_root.glob("sub-*") if d.is_dir())


def scans(runs: Iterable[ScanRun]) -> list[str]:
    """The scan kinds, tasks first (alphabetically), then anatomicals."""
    return sorted({r.scan for r in runs}, key=lambda s: (not s.startswith("task-"), s))


# ---- reading the values -------------------------------------------------------------------------


def fmt_value(value: object, scale: float = 1.0) -> str:
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return " ".join(fmt_value(v, scale) for v in value)  # lists; pydicom MultiValue
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        number = float(value) * scale
        return str(int(number)) if number.is_integer() else f"{number:.6g}"
    try:  # pydicom's DSfloat / IS behave as numbers
        number = float(str(value)) * scale
    except ValueError:
        return str(value).strip()
    return str(int(number)) if number.is_integer() else f"{number:.6g}"


def slice_timing_summary(times: list[float]) -> str:
    """'60 slices, 30 shots (MB 2), interleaved' from the sidecar's SliceTiming."""
    if not times:
        return ""
    values = np.round(np.asarray(times, float), 4)
    shot_times = np.unique(values)
    shots = len(shot_times)
    # The lowest slice excited in each shot, in time order.
    firsts = np.array([np.flatnonzero(values == t)[0] for t in shot_times])
    steps = np.diff(firsts)
    if not len(steps):
        pattern = "all at once"
    elif np.all(steps == 1):
        pattern = "ascending"
    elif np.all(steps == -1):
        pattern = "descending"
    else:
        pattern = "interleaved"
    parts = [f"{len(values)} slices"]
    if shots < len(values):
        parts.append(f"{shots} shots (MB {len(values) // shots})")
    parts.append(pattern)
    return ", ".join(parts)


def header_values(nifti: Path) -> dict[str, Value]:
    try:
        header = cast(nib.Nifti1Header, nib.load(nifti).header)  # type: ignore[attr-defined]
    except (OSError, ValueError, ImageFileError) as e:
        log.warning("%s: header not read (%s)", nifti.name, e)
        return {}
    shape = header.get_data_shape()
    zooms = header.get_zooms()
    values = {
        "Matrix (voxels)": " x ".join(str(n) for n in shape[:3]),
        "Voxel size (mm)": " x ".join(f"{z:.3g}" for z in zooms[:3]),
        "Volumes": str(shape[3]) if len(shape) > 3 else "1",
        "Orientation": "".join(nib.aff2axcodes(header.get_best_affine())),
        "Data type": str(header.get_data_dtype()),
    }
    if len(shape) > 3 and len(zooms) > 3 and zooms[3] > 0:
        tr = float(zooms[3])
        if header.get_xyzt_units()[1] == "msec":
            tr /= 1000.0
        values["TR in header (s)"] = fmt_value(tr)
        values["Duration (s)"] = fmt_value(round(tr * shape[3], 1))
    return {key: Value(text, HEADER) for key, text in values.items()}


def read_sidecar(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def sidecar_values(sidecar: dict[str, object]) -> dict[str, Value]:
    values = {}
    for param in PARAMS:
        if param.sidecar is None or param.sidecar not in sidecar:
            continue
        raw = sidecar[param.sidecar]
        if param.sidecar == "SliceTiming" and isinstance(raw, list):
            text = slice_timing_summary([float(t) for t in raw])
        else:
            text = fmt_value(raw)
        if text:
            values[param.label] = Value(text, SIDECAR)
    return values


def read_dicom_header(series_dir: Path) -> dict[str, object]:
    """The whitelisted DICOM_TAGS of the first readable file in a series folder."""
    import pydicom  # here, so the rest of the tab works without it
    from pydicom.errors import InvalidDicomError

    files = sorted(f for f in series_dir.iterdir() if f.is_file())
    for path in files[:DICOM_FILES_TRIED]:
        try:
            ds = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=list(DICOM_TAGS))
        except (InvalidDicomError, OSError, ValueError, AttributeError):
            continue
        return {tag: ds.get(tag) for tag in DICOM_TAGS if ds.get(tag) not in (None, "")}
    return {}


def dicom_values(tags: dict[str, object]) -> dict[str, Value]:
    values = {}
    for param in (*PARAMS, *DICOM_ONLY):
        if param.dicom is None or param.dicom not in tags:
            continue
        raw = tags[param.dicom]
        if param.dicom == "Rows" and "Columns" in tags:
            text = f"{fmt_value(raw)} x {fmt_value(tags['Columns'])}"
        elif param.dicom == "AcquisitionMatrix":  # freq rows, freq cols, phase rows, phase cols
            text = fmt_value([v for v in raw if v])  # type: ignore[union-attr]
        else:
            text = fmt_value(raw, param.dicom_scale)
        if text:
            values[param.label] = Value(text, DICOM)
    return values


def read_values(runs: list[ScanRun]) -> None:
    """NIfTI header and sidecar values (the DICOM ones keep: they are only added)."""
    for run in runs:
        found = {**header_values(run.nifti), **sidecar_values(read_sidecar(run.sidecar))}
        dicom = {k: v for k, v in run.values.items() if v.source == DICOM and k not in found}
        run.values = {**found, **dicom}


# ---- the DICOM series of a run ------------------------------------------------------------------


def dicom_series(dicom_root: Path, bids_root: Path, subject: str) -> dict[str, list[Path]]:
    """{BIDS stem: candidate series folders} for one subject, as the converter matched them.

    A scan with several series was converted as run-1, run-2, ...; once one is picked at
    crosscheck it has no run number, so the stem without one lists them all.
    """
    found = find_sessions(
        dicom_root,
        read_dicom_corrections(bids_root),
        read_excluded_sessions(bids_root),
        read_dicom_date_corrections(bids_root),
    )
    numbered = number_sessions(
        [s for s in found if s.subject_id == subject], read_dicom_session_corrections(bids_root)
    )
    rules = read_scan_rules(bids_root)
    stems: dict[str, list[Path]] = {}
    for session in numbered.get(subject, []):
        for rule, series in match_series(session.dicom.folder, rules).items():
            stems[bids_stem(session.prefix, rule.entities, rule.suffix)] = series
            if len(series) > 1:
                for n, folder in enumerate(series, start=1):
                    stems[bids_stem(session.prefix, rule.entities, rule.suffix, n)] = [folder]
    return stems


def series_of_run(
    nifti: Path, stems: dict[str, list[Path]]
) -> tuple[Path | None, dict[str, object], str]:
    """(series folder, its DICOM_TAGS, '') for a BIDS run, from dicom_series()'s stems; or
    (None, {}, why not). Several candidate series (picked at crosscheck): the sidecar's
    SeriesNumber tells which; without a match it is left out rather than guessed."""
    stem = nifti.name.removesuffix(".nii.gz")
    candidates = stems.get(stem, [])
    if not candidates:
        return None, {}, "no DICOM series found"
    if len(candidates) == 1:
        return candidates[0], read_dicom_header(candidates[0]), ""
    wanted = read_sidecar(nifti.with_name(f"{stem}.json")).get("SeriesNumber")
    for folder in candidates:
        tags = read_dicom_header(folder)
        if wanted is not None and str(tags.get("SeriesNumber")) == str(wanted):
            return folder, tags, ""
    return (
        None,
        {},
        f"{len(candidates)} DICOM series could be this run; not guessed "
        "(sidecar has no matching SeriesNumber)",
    )


def add_dicom_values(runs: list[ScanRun], dicom_root: Path, bids_root: Path) -> None:
    """Read each run's DICOM series: fill the parameters the header and sidecar lack."""
    if not runs:
        return
    stems = dicom_series(dicom_root, bids_root, runs[0].subject)
    for run in runs:
        series, tags, why_not = series_of_run(run.nifti, stems)
        if series is None:
            run.dicom_note = why_not
            continue
        if not tags:
            run.dicom_note = f"{series.name}: no readable DICOM file"
            continue
        run.dicom_note = series.name
        for label, value in dicom_values(tags).items():
            if label not in run.values:
                run.values[label] = value


# ---- the table ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    label: str
    group: str
    cells: list[str]  # text with its source mark; "" when missing
    differs: bool
    incomplete: bool  # found for some runs only


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def table_rows(runs: list[ScanRun]) -> list[Row]:
    """Every parameter found for at least one run, grouped as PARAMS lists them."""
    params = [
        *(Param(label, HEADER_GROUP) for label in HEADER_PARAMS),
        *PARAMS,
        *DICOM_ONLY,
    ]
    rows = []
    for param in params:
        values = [run.values.get(param.label) for run in runs]
        if not any(values):
            continue
        present = {normalise(v.text) for v in values if v is not None}
        rows.append(
            Row(
                param.label,
                param.group,
                [v.text + SOURCE_MARKS[v.source] if v else "" for v in values],
                differs=param.compare and len(present) > 1,
                incomplete=param.compare and None in values,
            )
        )
    return rows


def clip(text: str, width: int = MAX_CELL) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def render_table(runs: list[ScanRun], rows: list[Row], only_differences: bool = False) -> list[str]:
    """The table as a terminal-style log: one line per parameter, '≠' where runs differ."""
    if not runs:
        return ["No runs of this scan."]
    shown = [r for r in rows if r.differs or r.incomplete] if only_differences else rows
    label_width = max([len("Parameter"), *(len(r.label) for r in rows)]) + 2
    widths = [
        max([len(run.column), *(len(clip(r.cells[i])) for r in rows)]) + 2
        for i, run in enumerate(runs)
    ]
    rule = "-" * (2 + label_width + sum(widths))
    lines = [
        "  " + "Parameter".ljust(label_width)
        + "".join(run.column.ljust(w) for run, w in zip(runs, widths, strict=True)),
        rule,
    ]  # fmt: skip
    group = None
    for row in shown:
        if row.group != group:
            group = row.group
            lines.append(f"  [{group}]")
        mark = "≠ " if row.differs else ("? " if row.incomplete else "  ")
        cells = "".join(
            clip(cell or "-").ljust(w) for cell, w in zip(row.cells, widths, strict=True)
        )
        lines.append(mark + row.label.ljust(label_width) + cells.rstrip())
    if only_differences and not shown:
        lines.append("  (no parameter differs)")
    lines.append(rule)
    return lines


def summary_lines(runs: list[ScanRun], rows: list[Row]) -> list[str]:
    differ = [r.label for r in rows if r.differs]
    missing = [r.label for r in rows if r.incomplete and not r.differs]
    lines = [
        f"{len(runs)} run(s): " + ", ".join(f"{r.column} = {r.nifti.name}" for r in runs),
    ]
    lines.append(
        f"≠ {len(differ)} parameter(s) differ: {', '.join(differ)}"
        if differ
        else "= All parameters found agree."
    )
    if missing:
        lines.append(f"? Found for some runs only: {', '.join(missing)}")
    notes = [f"{r.column}: {r.dicom_note}" for r in runs if r.dicom_note]
    if notes:
        lines.append("DICOM series: " + "; ".join(notes))
    lines.append(SOURCE_LEGEND)
    return lines


def write_csv(runs: list[ScanRun], rows: list[Row], path: Path) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Group", "Parameter", *(r.column for r in runs), "Differs"])
        for row in rows:
            writer.writerow([row.group, row.label, *row.cells, "yes" if row.differs else ""])
