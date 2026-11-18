"""The MRIQC tab's own look at one scan, drawn from the BIDS NIfTI that MRIQC measured.

MRIQC's HTML reports are not shown: they need a web engine in the app (slow to start, and it
crashed on quick clicking) and their interactive parts invite work outside the app. What a
rater needs to see is drawn here instead:

* BOLD: the mean image and the tSNR map (axial slices), the global signal and DVARS over
  time, and a carpet plot (every brain voxel's signal over time, a sample of them per row).
* T1w: sagittal, coronal and axial slices (as for C-PAC's outputs, brain_slices.py).
"""

from pathlib import Path

import nibabel as nib
import numpy as np
from matplotlib.figure import Figure
from matplotlib.gridspec import SubplotSpec
from nibabel.filebasedimages import ImageFileError

from fmri_local_process.processing.brain_slices import (
    SlicePreview,
    display_range,
    render_slices,
    slice_indices,
)

MOSAIC_SLICES = 8
CARPET_ROWS = 400  # voxels shown in the carpet plot (a sample spread over the brain)
CARPET_LIMIT = 2.0  # z-scores beyond this saturate the carpet's colours
HEAD_FRACTION = 0.3  # of the bright end of the mean image: voxels above it count as head
TEXT_COLOR = "white"
DVARS_COLOR = "#ff7f0e"
SIGNAL_COLOR = "#4fc3f7"


class ViewError(ValueError):
    """The scan can't be drawn (unreadable, or not the shape expected)."""


def load_series(path: Path) -> tuple[np.ndarray, tuple[float, float, float]]:
    """The whole BOLD series as float32, reoriented to RAS+, and its voxel size."""
    try:
        image = nib.as_closest_canonical(nib.load(path))
        data = np.asarray(image.dataobj, dtype=np.float32)
    except (OSError, ValueError, EOFError, ImageFileError) as e:
        raise ViewError(f"{path.name} can't be read: {e}") from e
    if data.ndim != 4 or data.shape[3] < 3:
        raise ViewError(f"{path.name} isn't a BOLD series (shape {data.shape}).")
    x, y, z = (float(zoom) for zoom in image.header.get_zooms()[:3])
    return data, (x, y, z)


def head_mask(mean: np.ndarray) -> np.ndarray:
    """Voxels bright enough in the mean image to be head (not the background)."""
    signal = mean[mean > 0]
    if not signal.size:
        return np.zeros(mean.shape, bool)
    return mean > HEAD_FRACTION * float(np.percentile(signal, 98))


def tsnr_map(data: np.ndarray) -> np.ndarray:
    """Mean over standard deviation in time, per voxel (0 where the signal is flat)."""
    mean, std = data.mean(axis=3), data.std(axis=3)
    return np.divide(mean, std, out=np.zeros_like(mean), where=std > 0)


def global_signal(data: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """The head's mean signal per volume, as % change from its average."""
    series = data[mask].mean(axis=0)
    average = float(series.mean()) or 1.0
    return 100.0 * (series - average) / average


def dvars(data: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Volume-to-volume change in the head (RMS of the differences), as % of the mean signal;
    0 for the first volume."""
    voxels = data[mask]
    if not voxels.size:
        return np.zeros(data.shape[3])
    change = np.sqrt(np.mean(np.diff(voxels, axis=1) ** 2, axis=0))
    return np.concatenate([[0.0], 100.0 * change / (float(voxels.mean()) or 1.0)])


def carpet(data: np.ndarray, mask: np.ndarray, rows: int = CARPET_ROWS) -> np.ndarray:
    """(voxels, time): a spread-out sample of the head's voxels, each z-scored over time."""
    voxels = data[mask]
    if voxels.shape[0] > rows:
        voxels = voxels[np.linspace(0, voxels.shape[0] - 1, rows).astype(int)]
    mean = voxels.mean(axis=1, keepdims=True)
    std = voxels.std(axis=1, keepdims=True)
    return np.divide(voxels - mean, std, out=np.zeros_like(voxels), where=std > 0)


def draw_mosaic(
    figure: Figure,
    cell: SubplotSpec,
    volume: np.ndarray,
    zooms: tuple[float, float, float],
    title: str,
    cmap: str,
) -> None:
    """A row of axial slices spread over the part of the volume that holds signal."""
    grid = cell.subgridspec(1, MOSAIC_SLICES, wspace=0.02)
    vmin, vmax = display_range(volume)
    indices = slice_indices(volume, 2, MOSAIC_SLICES)
    for column in range(MOSAIC_SLICES):
        axes = figure.add_subplot(grid[0, column])
        axes.set_axis_off()
        if column < len(indices):
            axes.imshow(
                volume[:, :, indices[column]].T,
                cmap=cmap,
                origin="lower",
                vmin=vmin,
                vmax=vmax,
                aspect=zooms[1] / zooms[0],
                interpolation="nearest",
            )
        if column == 0:
            axes.set_title(title, color=TEXT_COLOR, fontsize=9, loc="left")


def bold_figure(path: Path) -> Figure:
    data, zooms = load_series(path)
    mean = data.mean(axis=3)
    mask = head_mask(mean)
    figure = Figure(figsize=(12, 9.5), facecolor="black")
    grid = figure.add_gridspec(4, 1, height_ratios=(1.1, 1.1, 0.9, 1.6), hspace=0.35)
    draw_mosaic(figure, grid[0], mean, zooms, "Mean image (look for ghosts, signal loss)", "gray")
    draw_mosaic(figure, grid[1], tsnr_map(data), zooms, "tSNR (dark patches: noisy)", "inferno")

    times = np.arange(data.shape[3])
    lines = figure.add_subplot(grid[2])
    lines.set_facecolor("black")
    lines.plot(times, global_signal(data, mask), color=SIGNAL_COLOR, linewidth=0.9)
    lines.set_ylabel("global signal (%)", color=SIGNAL_COLOR, fontsize=8)
    jumps = lines.twinx()
    jumps.plot(times, dvars(data, mask), color=DVARS_COLOR, linewidth=0.9)
    jumps.set_ylabel("DVARS (%)", color=DVARS_COLOR, fontsize=8)
    for axes in (lines, jumps):
        axes.tick_params(colors=TEXT_COLOR, labelsize=7)
        axes.set_xlim(0, times[-1])
    lines.set_title(
        "Signal over time (spikes in DVARS: sudden movement or artefacts)",
        color=TEXT_COLOR,
        fontsize=9,
        loc="left",
    )

    carpet_axes = figure.add_subplot(grid[3])
    carpet_axes.imshow(
        carpet(data, mask),
        cmap="gray",
        aspect="auto",
        vmin=-CARPET_LIMIT,
        vmax=CARPET_LIMIT,
        interpolation="nearest",
        extent=(0, times[-1], 0, 1),
    )
    carpet_axes.set_yticks([])
    carpet_axes.tick_params(colors=TEXT_COLOR, labelsize=7)
    carpet_axes.set_xlabel("volume", color=TEXT_COLOR, fontsize=8)
    carpet_axes.set_title(
        "Carpet plot: head voxels over time (vertical bands: motion or artefacts)",
        color=TEXT_COLOR,
        fontsize=9,
        loc="left",
    )
    figure.suptitle(
        f"{path.name}   ({data.shape[3]} volumes)", color=TEXT_COLOR, fontsize=9, x=0.01, ha="left"
    )
    figure.subplots_adjust(left=0.06, right=0.94, bottom=0.05, top=0.94)
    return figure


def t1w_figure(path: Path) -> Figure:
    try:
        return render_slices(SlicePreview("T1w", path, (), "T1w"))
    except (OSError, ValueError, EOFError, ImageFileError) as e:
        raise ViewError(f"{path.name} can't be read: {e}") from e


def run_figure(path: Path, modality: str) -> Figure:
    """The look at one scan: BOLD or T1w. Raises ViewError when it can't be drawn."""
    return bold_figure(path) if modality == "bold" else t1w_figure(path)
