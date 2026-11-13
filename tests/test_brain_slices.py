"""Brain-slice previews on small synthetic NIfTI volumes."""

from pathlib import Path

import nibabel as nib
import numpy as np

from fmri_local_process.processing.brain_slices import (
    display_range,
    load_volume,
    output_kind,
    overlay_slice,
    render_slices,
    slice_indices,
    slice_previews,
)

SES = "output/pipeline_cpac_preproc/sub-P01/ses-1"


def box(shape: tuple[int, int, int], value: float = 1.0) -> np.ndarray:
    data = np.zeros(shape, dtype=np.float32)
    data[2:-2, 2:-2, 2:-2] = value
    return data


def save(path: Path, data: np.ndarray, affine: np.ndarray | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    nib.Nifti1Image(data, np.eye(4) if affine is None else affine).to_filename(path)
    return path


def make_outputs(out: Path) -> None:
    anat, func = out / SES / "anat", out / SES / "func"
    save(anat / "sub-P01_ses-1_desc-preproc_T1w.nii.gz", box((12, 14, 10), 300))
    save(anat / "sub-P01_ses-1_desc-brain_mask.nii.gz", box((12, 14, 10)))
    save(anat / "sub-P01_ses-1_label-GM_mask.nii.gz", box((12, 14, 10)))
    for task in ("Rest", "Matching"):
        save(func / f"sub-P01_ses-1_task-{task}_desc-mean_bold.nii.gz", box((8, 8, 6), 50))
        save(func / f"sub-P01_ses-1_task-{task}_desc-brain_mask.nii.gz", box((8, 8, 6)))
        save(func / f"sub-P01_ses-1_task-{task}_space-T1w_sbref.nii.gz", box((12, 14, 10), 9))


def test_output_kind() -> None:
    assert output_kind("sub-1_ses-2_task-Rest_space-T1w_sbref.nii.gz") == "space-T1w_sbref"
    assert output_kind("sub-1_ses-2_desc-preproc_T1w.nii.gz") == "desc-preproc_T1w"


def test_slice_previews_pair_masks_and_follow_task(tmp_path: Path) -> None:
    make_outputs(tmp_path)
    previews = {preview.label: preview for preview in slice_previews(tmp_path, "P01")}
    assert list(previews) == [
        "ses-1: T1w + brain mask",
        "ses-1: T1w + tissue masks",
        "ses-1 Matching: Mean BOLD + BOLD mask",
        "ses-1 Rest: Mean BOLD + BOLD mask",
        "ses-1 Matching: BOLD ref on T1w (coregistration)",
        "ses-1 Rest: BOLD ref on T1w (coregistration)",
    ]
    rest_bold = previews["ses-1 Rest: Mean BOLD + BOLD mask"]
    [(mask, name)] = rest_bold.overlays
    assert mask.name == "sub-P01_ses-1_task-Rest_desc-brain_mask.nii.gz" and name == "BOLD mask"
    tissue = previews["ses-1: T1w + tissue masks"]
    assert [name for _path, name in tissue.overlays] == ["GM"]  # WM/CSF absent: skipped

    rest_only = [preview.label for preview in slice_previews(tmp_path, "P01", "Rest")]
    assert not any("Matching" in label for label in rest_only)
    assert "ses-1: T1w + brain mask" in rest_only  # anat isn't per task


def test_overlay_is_resampled_through_the_affines(tmp_path: Path) -> None:
    base = load_volume(save(tmp_path / "base.nii.gz", np.zeros((8, 8, 8), dtype=np.float32)))
    coarse = np.zeros((4, 4, 4), dtype=np.float32)
    coarse[1, 1, 1] = 1
    # 2 mm voxels shifted half a base voxel: coarse voxel 1 covers base voxels 1 and 2.
    affine = np.diag([2.0, 2.0, 2.0, 1.0])
    affine[:3, 3] = -0.5
    mask = load_volume(save(tmp_path / "mask.nii.gz", coarse, affine))
    axial = overlay_slice(base, mask, axis=2, index=2)
    assert axial.shape == (8, 8)
    assert set(zip(*np.nonzero(axial), strict=True)) == {(1, 1), (1, 2), (2, 1), (2, 2)}
    assert not overlay_slice(base, mask, axis=2, index=5).any()


def test_slice_indices_stay_within_the_signal() -> None:
    data = np.zeros((10, 10, 40), dtype=np.float32)
    data[:, :, 10:30] = 1
    indices = slice_indices(data, axis=2)
    assert 10 <= min(indices) and max(indices) <= 29


def test_display_range() -> None:
    assert display_range(np.zeros((3, 3, 3))) == (0.0, 1.0)
    assert display_range(box((8, 8, 8), 300)) == (0.0, 300.0)  # flat image still visible
    ramp = np.arange(1, 101, dtype=np.float32).reshape(4, 5, 5)
    low, high = display_range(ramp)
    assert 1 < low < 5 and 96 < high < 100


def test_slice_previews_skip_tissue_view_without_masks(tmp_path: Path) -> None:
    make_outputs(tmp_path)
    (tmp_path / SES / "anat" / "sub-P01_ses-1_label-GM_mask.nii.gz").unlink()
    labels = [preview.label for preview in slice_previews(tmp_path, "P01")]
    assert "ses-1: T1w + tissue masks" not in labels


def test_render_slices_draws_every_view(tmp_path: Path) -> None:
    make_outputs(tmp_path)
    preview = slice_previews(tmp_path, "P01")[0]
    figure = render_slices(preview)
    images = [axes for axes in figure.axes if axes.images]
    assert len(images) >= 3 * 4  # three views, several slices each
    assert any(axes.collections for axes in images)  # mask outline drawn


def test_render_slices_handles_4d(tmp_path: Path) -> None:
    series = np.stack([box((8, 8, 6), 10)] * 3, axis=-1)
    path = save(
        tmp_path / "out" / SES / "func" / "sub-P01_ses-1_task-Rest_desc-mean_bold.nii.gz", series
    )
    assert load_volume(path).data.shape == (8, 8, 6)
