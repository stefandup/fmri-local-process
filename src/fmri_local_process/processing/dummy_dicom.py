"""Dummy DICOM series: small images of noise around a sphere, written from scratch.

Of a real series only numbers are ever read: how many images, volumes and planned volumes, and
the sequence settings in NUMERIC_TAGS. No text tag, no patient field, no pixel.
"""

import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from fmri_local_process.processing.dicom_volumes import count_series

NUMERIC_TAGS = ("RepetitionTime", "EchoTime", "FlipAngle", "MagneticFieldStrength")
SIZE = 24  # pixels a side
SLICES = 4  # of a BOLD volume
ANAT_SLICES = 20
VOXEL_MM = 8.0
DEFAULT_TR_MS = 832.0


@dataclass
class SeriesShape:
    """What a dummy series is made from."""

    volumes: int = 1
    planned: int | None = None
    missing: int = 0  # images lost between the scanner and the folder
    tr_ms: float = DEFAULT_TR_MS
    te_ms: float = 30.0
    flip: float = 52.0
    field_t: float = 3.0


def read_shape(folder: Path, bold: bool) -> SeriesShape:
    """A real series' shape; a plain one when it can't be read."""
    import pydicom  # here, so the rest of the toolbox works without it
    from pydicom.errors import InvalidDicomError

    shape = SeriesShape()
    files = sorted(f for f in folder.iterdir() if f.is_file())
    for path in files[:5]:
        try:
            ds = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=list(NUMERIC_TAGS))
            tr, te, flip, field = (float(ds.get(tag) or 0) for tag in NUMERIC_TAGS)
        except (InvalidDicomError, OSError, ValueError, TypeError, AttributeError):
            continue
        shape.tr_ms, shape.te_ms = tr or shape.tr_ms, te or shape.te_ms
        shape.flip, shape.field_t = flip or shape.flip, field or shape.field_t
        break
    if bold:
        counts = count_series(folder)
        full = counts.expected_images or counts.images  # with the missing ones
        shape.volumes = max(1, full // max(counts.positions, 1))
        shape.planned = counts.planned
        shape.missing = min(counts.missing_images, SLICES * 2)
    return shape


def write_series(
    folder: Path,
    shape: SeriesShape,
    subject: str,
    number: int,
    rng: random.Random,
    prefix: str = "IM",
    images: bool = True,
) -> int:
    """Write the series (images=False: one image, enough for what only looks at folders); how
    many files."""
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

    folder.mkdir(parents=True, exist_ok=True)
    noise = np.random.default_rng(rng.randrange(2**32))
    slices = 1 if not images else SLICES if shape.volumes > 1 else ANAT_SLICES
    volumes = shape.volumes if images else 1
    total = slices * volumes
    inner = range(2, total)  # never the first or last image: a gap shows between two
    lost = set(rng.sample(inner, min(shape.missing, len(inner) // 2))) if images else set()
    study, series, frame = generate_uid(), generate_uid(), generate_uid()
    grid = np.indices((slices, SIZE, SIZE)).reshape(3, -1).T
    centre = np.array([(slices - 1) / 2, (SIZE - 1) / 2, (SIZE - 1) / 2])
    radius = np.array([max(slices / 2, 1), SIZE / 2.6, SIZE / 2.8])
    inside = (((grid - centre) / radius) ** 2).sum(axis=1).reshape(slices, SIZE, SIZE) < 1
    written = 0
    for volume in range(volumes):
        for index in range(slices):
            instance = volume * slices + index + 1
            if instance in lost:
                continue
            meta = FileMetaDataset()
            meta.MediaStorageSOPClassUID = MRImageStorage
            meta.MediaStorageSOPInstanceUID = generate_uid()
            meta.TransferSyntaxUID = ExplicitVRLittleEndian
            path = folder / f"{prefix}{instance:05d}.dcm"
            ds: Dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
            ds.SOPClassUID = MRImageStorage
            ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
            ds.StudyInstanceUID, ds.SeriesInstanceUID = study, series
            ds.FrameOfReferenceUID = frame
            ds.Modality, ds.Manufacturer = "MR", "Dummy"
            ds.PatientName, ds.PatientID = f"Dummy^{subject}", subject
            ds.SeriesDescription = ds.ProtocolName = "dummy"
            ds.SeriesNumber, ds.InstanceNumber = number, instance
            ds.AcquisitionNumber = ds.TemporalPositionIdentifier = volume + 1
            if shape.planned:
                ds.NumberOfTemporalPositions = shape.planned
            ds.ImageType = ["ORIGINAL", "PRIMARY", "M"]
            ds.ScanningSequence, ds.MRAcquisitionType = "EP" if volumes > 1 else "GR", "2D"
            ds.RepetitionTime, ds.EchoTime = f"{shape.tr_ms:g}", f"{shape.te_ms:g}"
            ds.FlipAngle, ds.MagneticFieldStrength = f"{shape.flip:g}", f"{shape.field_t:g}"
            ds.SliceThickness = ds.SpacingBetweenSlices = f"{VOXEL_MM:g}"
            ds.PixelSpacing = [f"{VOXEL_MM:g}"] * 2
            ds.ImageOrientationPatient = ["1", "0", "0", "0", "1", "0"]
            corner = -VOXEL_MM * SIZE / 2
            position = VOXEL_MM * (index - slices / 2)
            ds.ImagePositionPatient = [f"{corner:g}", f"{corner:g}", f"{position:g}"]
            ds.SliceLocation = f"{position:g}"
            ds.Rows = ds.Columns = SIZE
            ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
            ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
            values = noise.normal(40, 15, (SIZE, SIZE)) + 800 * inside[index]
            values += noise.normal(0, 30, (SIZE, SIZE)) * inside[index]
            ds.PixelData = np.clip(values, 0, 4095).astype("<u2").tobytes()
            ds.save_as(path, enforce_file_format=True)
            written += 1
    return written
