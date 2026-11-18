"""The MRIQC tab's own scan views and QC ratings, on synthetic NIfTIs (no MRIQC, no real data)."""

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from fmri_local_process.processing import mriqc as mq
from fmri_local_process.processing import mriqc_views as mv


def write_nifti(path: Path, data: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    nib.save(nib.Nifti1Image(data.astype(np.float32), np.diag([3.0, 3.0, 3.5, 1.0])), path)
    return path


def bold_series(volumes: int = 20) -> np.ndarray:
    """A bright block of 'head' in a dark field, with noise, and one volume that jumps."""
    rng = np.random.default_rng(0)
    data = rng.normal(5.0, 1.0, (12, 12, 8, volumes))
    data[3:9, 3:9, 2:6, :] += 100.0
    data[3:9, 3:9, 2:6, 10] += 30.0  # a spike
    return data


def test_bold_measures_find_the_head_and_the_spike() -> None:
    data = bold_series()
    mean = data.mean(axis=3)
    mask = mv.head_mask(mean)
    assert mask.sum() == 6 * 6 * 4  # the block, not the background
    jumps = mv.dvars(data, mask)
    assert jumps.shape == (20,) and jumps[0] == 0
    assert int(np.argmax(jumps)) in (10, 11)  # into and out of the spike
    assert mv.global_signal(data, mask)[10] > 10  # % above the run's average
    assert mv.tsnr_map(data)[5, 5, 3] > mv.tsnr_map(data)[0, 0, 0]
    rows = mv.carpet(data, mask, rows=50)
    assert rows.shape == (50, 20)
    assert np.allclose(rows.mean(axis=1), 0, atol=1e-4)  # each voxel z-scored


def test_views_draw_bold_and_t1w_and_say_why_not(tmp_path: Path) -> None:
    bold = write_nifti(tmp_path / "sub-01_task-rest_bold.nii.gz", bold_series())
    figure = mv.run_figure(bold, "bold")
    assert len(figure.axes) >= 2 * mv.MOSAIC_SLICES + 2  # two mosaics, the lines, the carpet

    t1w = write_nifti(tmp_path / "sub-01_T1w.nii.gz", bold_series()[..., 0])
    assert mv.run_figure(t1w, "T1w").axes  # sagittal / coronal / axial slices

    with pytest.raises(mv.ViewError, match="isn't a BOLD series"):
        mv.run_figure(t1w, "bold")
    empty = tmp_path / "empty_bold.nii.gz"
    empty.write_bytes(b"")
    with pytest.raises(mv.ViewError, match="can't be read"):
        mv.run_figure(empty, "bold")


def test_ratings_are_saved_changed_and_removed(tmp_path: Path) -> None:
    assert mq.read_ratings(tmp_path) == {}
    saved = mq.save_rating(tmp_path, "sub-01_task-rest_bold", 1, "ghosting\tall over\n")
    assert saved is not None and saved.text == "1 bad: discard"
    mq.save_rating(tmp_path, "sub-02_task-rest_bold", None, "ask Sam")  # a comment alone
    ratings = mq.read_ratings(tmp_path)
    assert ratings["sub-01_task-rest_bold"].comment == "ghosting all over"  # one line
    assert ratings["sub-02_task-rest_bold"].rating is None

    mq.save_rating(tmp_path, "sub-01_task-rest_bold", 3, "")
    assert mq.read_ratings(tmp_path)["sub-01_task-rest_bold"].text == "3 keep"
    assert mq.save_rating(tmp_path, "sub-02_task-rest_bold", None, "") is None
    assert list(mq.read_ratings(tmp_path)) == ["sub-01_task-rest_bold"]
    header = (tmp_path / mq.RATINGS_FILE).read_text(encoding="utf-8").splitlines()[0]
    assert header.split("\t") == list(mq.RATING_COLUMNS)  # opens as a table in Excel
