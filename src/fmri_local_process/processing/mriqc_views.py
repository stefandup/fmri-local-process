"""The MRIQC tab's own look at one scan, drawn from the BIDS NIfTI that MRIQC measured.

MRIQC's HTML reports are not shown: they need a web engine in the app (slow to start, and it
crashed on quick clicking) and their interactive parts invite work outside the app. What a
rater needs to see is drawn here instead:

* BOLD: the mean image and the tSNR map (every axial slice), the global signal and DVARS
  over time, and a carpet plot (every brain voxel's signal over time, a sample of them per
  row).
* T1w: every axial slice.

The figure grows with the scan (a row per MOSAIC_COLUMNS slices); the tab's zoom and scroll
look at it closely.
"""

from pathlib import Path

import nibabel as nib
import numpy as np
from matplotlib.figure import Figure
from matplotlib.gridspec import SubplotSpec
from nibabel.filebasedimages import ImageFileError

from fmri_local_process.processing.brain_slices import display_range, load_volume

MOSAIC_COLUMNS = 10  # slices to a row
FIGURE_WIDTH = 12.0  # inches; the height follows the number of slices
LEFT, RIGHT = 0.06, 0.94  # figure fractions: room for the line plot's two axes labels
SECTION_GAP = 0.45  # inches above each section, for its title
TOP_MARGIN, BOTTOM_MARGIN = 0.2, 0.45  # inches (the bottom one for the carpet's x labels)
LINES_HEIGHT, CARPET_HEIGHT = 1.6, 2.6  # inches
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


def filled_slices(volume: np.ndarray, axis: int = 2) -> list[int]:
    """Every slice from the first to the last that holds any signal (the empty FOV left out)."""
    profile = np.abs(volume).sum(axis=tuple(a for a in range(3) if a != axis))
    filled = np.flatnonzero(profile > 0)
    return list(range(int(filled[0]), int(filled[-1]) + 1)) if filled.size else []


def mosaic_height(volume: np.ndarray, zooms: tuple[float, float, float]) -> float:
    """Inches a mosaic of all the volume's axial slices takes, at MOSAIC_COLUMNS a row."""
    rows = max(1, -(-len(filled_slices(volume)) // MOSAIC_COLUMNS))  # ceiling division
    tile_width = FIGURE_WIDTH * (RIGHT - LEFT) / MOSAIC_COLUMNS
    tile_height = tile_width * (volume.shape[1] * zooms[1]) / (volume.shape[0] * zooms[0])
    return rows * tile_height


def draw_mosaic(
    figure: Figure,
    cell: SubplotSpec,
    volume: np.ndarray,
    zooms: tuple[float, float, float],
    title: str,
    cmap: str,
) -> None:
    """Every axial slice that holds signal, foot to head, MOSAIC_COLUMNS to a row."""
    indices = filled_slices(volume)
    rows = max(1, -(-len(indices) // MOSAIC_COLUMNS))
    grid = cell.subgridspec(rows, MOSAIC_COLUMNS, wspace=0.02, hspace=0.02)
    vmin, vmax = display_range(volume)
    for position in range(rows * MOSAIC_COLUMNS):
        axes = figure.add_subplot(grid[divmod(position, MOSAIC_COLUMNS)])
        axes.set_axis_off()
        if position == 0:
            axes.set_title(title, color=TEXT_COLOR, fontsize=9, loc="left")
        if position >= len(indices):
            continue
        axes.imshow(
            volume[:, :, indices[position]].T,
            cmap=cmap,
            origin="lower",
            vmin=vmin,
            vmax=vmax,
            aspect=zooms[1] / zooms[0],
            interpolation="nearest",
        )
        axes.text(
            0.03, 0.03, str(indices[position]), color=TEXT_COLOR, fontsize=6,
            transform=axes.transAxes,
        )  # fmt: skip


def stacked_figure(heights: list[float]) -> tuple[Figure, list[SubplotSpec]]:
    """A figure FIGURE_WIDTH wide with a section per height (inches), a titled gap above each."""
    ratios = [h for height in heights for h in (SECTION_GAP, height)]
    total = sum(ratios) + TOP_MARGIN + BOTTOM_MARGIN
    figure = Figure(figsize=(FIGURE_WIDTH, total), facecolor="black")
    grid = figure.add_gridspec(len(ratios), 1, height_ratios=ratios, hspace=0)
    figure.subplots_adjust(
        left=LEFT, right=RIGHT, top=1 - TOP_MARGIN / total, bottom=BOTTOM_MARGIN / total
    )
    return figure, [grid[index] for index in range(1, len(ratios), 2)]


def bold_figure(path: Path) -> Figure:
    data, zooms = load_series(path)
    mean = data.mean(axis=3)
    mask = head_mask(mean)
    mosaic = mosaic_height(mean, zooms)
    figure, (mean_cell, tsnr_cell, lines_cell, carpet_cell) = stacked_figure(
        [mosaic, mosaic, LINES_HEIGHT, CARPET_HEIGHT]
    )
    draw_mosaic(figure, mean_cell, mean, zooms, "Mean image (look for ghosts, signal loss)", "gray")
    draw_mosaic(figure, tsnr_cell, tsnr_map(data), zooms, "tSNR (dark patches: noisy)", "inferno")

    times = np.arange(data.shape[3])
    lines = figure.add_subplot(lines_cell)
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

    carpet_axes = figure.add_subplot(carpet_cell)
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
        f"{path.name}   ({data.shape[3]} volumes, {len(filled_slices(mean))} slices)",
        color=TEXT_COLOR,
        fontsize=9,
        x=0.01,
        ha="left",
    )
    return figure


def t1w_figure(path: Path) -> Figure:
    try:
        volume = load_volume(path)
    except (OSError, ValueError, EOFError, ImageFileError) as e:
        raise ViewError(f"{path.name} can't be read: {e}") from e
    figure, (cell,) = stacked_figure([mosaic_height(volume.data, volume.zooms)])
    draw_mosaic(
        figure, cell, volume.data, volume.zooms, "Every axial slice (look for motion, ringing)",
        "gray",
    )  # fmt: skip
    figure.suptitle(
        f"{path.name}   ({len(filled_slices(volume.data))} slices)",
        color=TEXT_COLOR,
        fontsize=9,
        x=0.01,
        ha="left",
    )
    return figure


def run_figure(path: Path, modality: str) -> Figure:
    """The look at one scan: BOLD or T1w. Raises ViewError when it can't be drawn."""
    return bold_figure(path) if modality == "bold" else t1w_figure(path)
