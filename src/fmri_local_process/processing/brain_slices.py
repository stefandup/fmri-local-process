"""Brain-slice previews of C-PAC's anatomical and functional outputs.

Each preview is one volume (e.g. the preprocessed T1w) shown as a grid of sagittal,
coronal and axial slices, with mask outlines on top (e.g. the brain mask) -- the quick
look for a failed skull strip, a misregistered BOLD, or a bad template warp.
"""

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

import nibabel as nib
import numpy as np
from matplotlib.figure import Figure
from scipy.ndimage import map_coordinates

from fmri_local_process.processing.cpac_docker import TaskFilter, as_tasks
from fmri_local_process.processing.cpac_outputs import bids_entities, subject_files

SLICES_PER_VIEW = 6
VIEWS = (("Sagittal", 0), ("Coronal", 1), ("Axial", 2))
OVERLAY_COLORS = ("#ff4136", "#2ecc71", "#3498db")
LEGEND_STEP = 0.16  # figure-width fraction between legend entries
# Entities that only say whose file it is; what's left names the output itself.
OWNER_ENTITIES = ("sub", "ses", "task", "run")


@dataclass(frozen=True)
class SliceSpec:
    """One kind of preview: a base volume and the masks outlined on it.

    Patterns match a file's name without its owner entities and extension, e.g.
    'space-MNI*_desc-preproc_T1w'. Overlays are (folder, pattern, legend name).
    """

    title: str
    folder: str
    pattern: str
    overlays: tuple[tuple[str, str, str], ...] = ()
    needs_overlay: bool = False  # skip the preview when none of its masks exist


SLICE_SPECS = (
    SliceSpec(
        "T1w + brain mask", "anat", "desc-preproc_T1w", (("anat", "desc-brain_mask", "brain mask"),)
    ),
    SliceSpec(
        "T1w + tissue masks",
        "anat",
        "desc-preproc_T1w",
        (
            ("anat", "label-GM_mask", "GM"),
            ("anat", "label-WM_mask", "WM"),
            ("anat", "label-CSF_mask", "CSF"),
        ),
        needs_overlay=True,
    ),
    SliceSpec(
        "T1w in template + brain mask",
        "anat",
        "space-MNI*_desc-preproc_T1w",
        (("anat", "space-MNI*_desc-brain_mask", "brain mask"),),
    ),
    SliceSpec(
        "Mean BOLD + BOLD mask",
        "func",
        "desc-mean_bold",
        (("func", "desc-brain_mask", "BOLD mask"),),
    ),
    SliceSpec(
        "BOLD ref on T1w (coregistration)",
        "func",
        "space-T1w_sbref",
        (("anat", "desc-brain_mask", "T1w brain mask"),),
    ),
    SliceSpec(
        "BOLD ref in template (normalisation)",
        "func",
        "space-MNI*_sbref",
        (("anat", "space-MNI*_desc-brain_mask", "template brain mask"),),
    ),
)


@dataclass(frozen=True)
class SlicePreview:
    label: str
    path: Path
    overlays: tuple[tuple[Path, str], ...] = field(default=())  # (mask file, legend name)
    kind: str = ""  # the SliceSpec title, e.g. 'T1w + brain mask'


def output_kind(name: str) -> str:
    """'sub-1_ses-1_task-Rest_space-T1w_sbref.nii.gz' -> 'space-T1w_sbref'."""
    parts = name.split(".", 1)[0].split("_")
    return "_".join(part for part in parts if part.split("-", 1)[0] not in OWNER_ENTITIES)


def find_in(folder: Path, pattern: str, task: str | None) -> list[Path]:
    """NIfTIs in folder whose output kind matches pattern (and, if given, of task)."""
    return [
        nifti
        for nifti in sorted(folder.glob("*.nii.gz"))
        if fnmatch.fnmatchcase(output_kind(nifti.name), pattern)
        and (task is None or bids_entities(nifti.name).get("task") == task)
    ]


def slice_previews(output_root: Path, subject: str, tasks: TaskFilter = None) -> list[SlicePreview]:
    """Every preview C-PAC's outputs allow for this subject (func ones of tasks), per session."""
    labels = as_tasks(tasks)
    previews = []
    session_dirs = sorted(
        {path.parent.parent for path in subject_files(output_root, subject, "*.nii.gz")}
    )
    for session_dir in session_dirs:
        session = session_dir.name.removeprefix("ses-")
        for spec in SLICE_SPECS:
            for base in find_in(session_dir / spec.folder, spec.pattern, None):
                base_task = bids_entities(base.name).get("task")
                if labels and spec.folder == "func" and base_task not in labels:
                    continue
                overlays = tuple(
                    (matches[0], name)
                    for folder, pattern, name in spec.overlays
                    if (
                        matches := find_in(
                            session_dir / folder, pattern, base_task if folder == "func" else None
                        )
                    )
                )
                if spec.needs_overlay and not overlays:
                    continue  # would only repeat the plain volume shown by another preview
                where = f"ses-{session} {base_task}" if base_task else f"ses-{session}"
                label = f"{where}: {spec.title}"
                previews.append(SlicePreview(label, base, overlays, spec.title))
    return previews


@dataclass
class Volume:
    data: np.ndarray  # 3D, RAS+ voxel order
    affine: np.ndarray
    zooms: tuple[float, float, float]


def load_volume(path: Path) -> Volume:
    """A NIfTI reoriented to RAS+, so every view is drawn the same way up; a 4D series
    gives its first volume (enough for a look, without decompressing the whole run)."""
    image = nib.as_closest_canonical(nib.load(path))
    proxy = image.dataobj
    data = np.asarray(proxy[..., 0] if len(image.shape) == 4 else proxy, dtype=np.float32)
    x, y, z = (float(zoom) for zoom in image.header.get_zooms()[:3])
    return Volume(data, np.asarray(image.affine), (x, y, z))


def slice_indices(data: np.ndarray, axis: int, count: int = SLICES_PER_VIEW) -> list[int]:
    """count slices spread over the part of axis that holds signal (not the empty FOV)."""
    profile = np.abs(data).sum(axis=tuple(a for a in range(3) if a != axis))
    filled = np.flatnonzero(profile > 0)
    first, last = (filled[0], filled[-1]) if filled.size else (0, data.shape[axis] - 1)
    fractions = np.linspace(0.15, 0.85, count)
    return sorted({int(round(first + f * (last - first))) for f in fractions})


def base_slice(volume: Volume, axis: int, index: int) -> np.ndarray:
    return np.take(volume.data, index, axis=axis)


def overlay_slice(base: Volume, overlay: Volume, axis: int, index: int) -> np.ndarray:
    """The overlay sampled on the base slice's voxels through both affines (nearest
    neighbour), so masks line up even on a different grid, e.g. a 3 mm BOLD mask on a 1 mm T1w."""
    grids = [np.arange(n) for n in base.data.shape]
    grids[axis] = np.array([index])
    ijk = np.stack([g.ravel() for g in np.meshgrid(*grids, indexing="ij")])
    world = base.affine[:3, :3] @ ijk + base.affine[:3, 3:]
    to_overlay = np.linalg.inv(overlay.affine)
    overlay_ijk = to_overlay[:3, :3] @ world + to_overlay[:3, 3:]
    values = map_coordinates(overlay.data, overlay_ijk, order=0, mode="constant", cval=0.0)
    shape = list(base.data.shape)
    shape[axis] = 1
    return values.reshape(shape).squeeze(axis)


def display_range(data: np.ndarray) -> tuple[float, float]:
    """Grey-scale limits: 2nd-98th percentile of the non-zero voxels, so a few very bright
    voxels don't wash the image out. A flat image (e.g. a binary mask) spans 0..its value."""
    signal = data[data > 0]
    if not signal.size:
        return 0.0, 1.0
    low, high = (float(value) for value in np.percentile(signal, (2, 98)))
    return (low, high) if high > low else (0.0, high)


def render_slices(preview: SlicePreview) -> Figure:
    """The preview as a sagittal / coronal / axial slice grid, mask outlines on top."""
    base = load_volume(preview.path)
    overlays = [(load_volume(path), name) for path, name in preview.overlays]
    vmin, vmax = display_range(base.data)

    figure = Figure(figsize=(2 * SLICES_PER_VIEW, 6.6), facecolor="black")
    axes_grid = figure.subplots(len(VIEWS), SLICES_PER_VIEW, squeeze=False)
    for row, (view, axis) in enumerate(VIEWS):
        shown = [a for a in range(3) if a != axis]  # (horizontal, vertical) volume axes
        aspect = base.zooms[shown[1]] / base.zooms[shown[0]]
        indices = slice_indices(base.data, axis)
        for column, axes in enumerate(axes_grid[row]):
            axes.set_axis_off()
            if column >= len(indices):
                continue
            index = indices[column]
            axes.imshow(
                base_slice(base, axis, index).T,
                cmap="gray",
                origin="lower",
                vmin=vmin,
                vmax=vmax,
                aspect=aspect,
                interpolation="nearest",
            )
            for (overlay, _name), color in zip(overlays, OVERLAY_COLORS, strict=False):
                mask = overlay_slice(base, overlay, axis, index).T > 0.5
                if mask.any() and not mask.all():
                    axes.contour(mask.astype(float), levels=[0.5], colors=color, linewidths=0.8)
            if column == 0:
                axes.set_title(view, color="white", fontsize=9, loc="left")
    figure.suptitle(preview.path.name, color="white", fontsize=9, x=0.01, y=0.99, ha="left")
    for position, ((_volume, name), color) in enumerate(
        zip(overlays, OVERLAY_COLORS, strict=False)
    ):
        x = 0.99 - LEGEND_STEP * (len(overlays) - 1 - position)
        figure.text(x, 0.955, f"— {name}", color=color, fontsize=9, ha="right")
    figure.subplots_adjust(left=0.01, right=0.99, bottom=0.01, top=0.92, wspace=0.02, hspace=0.12)
    return figure
