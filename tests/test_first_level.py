"""First-level GLM and atlas helpers, on small synthetic images (no Docker, no real data)."""

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from fmri_local_process.processing import atlases as at
from fmri_local_process.processing import first_level as fl

TR = 0.832
SHAPE = (8, 9, 7)
ACTIVE = (slice(2, 4), slice(3, 5), slice(2, 4))  # a 2x2x2 'visual cortex'


def write_run(
    root: Path, session: str, n_volumes: int, amplitude: float, baseline: float, seed: int
) -> Path:
    """A C-PAC-like MNI-space BOLD: noise everywhere, the checkerboard response in ACTIVE."""
    rng = np.random.default_rng(seed)
    design = fl.CHECKERBOARD.design
    task = fl.task_regressor(design, n_volumes, TR)
    data = baseline + rng.normal(0, 1.0, (*SHAPE, n_volumes))
    data[ACTIVE] += amplitude * task
    folder = root / "output" / "pipeline_cpac_preproc" / "sub-01" / f"ses-{session}" / "func"
    folder.mkdir(parents=True)
    name = f"sub-01_ses-{session}_task-Checkerboard_space-MNI152NLin6ASym_reg-2_desc-preproc_bold"
    affine = np.diag([3.0, 3.0, 3.0, 1.0])
    affine[:3, 3] = (-12.0, -100.0, -6.0)
    image = nib.Nifti1Image(data.astype(np.float32), affine)
    image.header.set_zooms((3.0, 3.0, 3.0, TR))
    path = folder / f"{name}.nii.gz"
    nib.save(image, path)
    (folder / "sub-01_ses-1_task-Checkerboard_reg-2_desc-boldSnr_quality.txt").write_text("42.5\n")
    return path


def test_on_blocks_follow_the_checkerboard_schedule() -> None:
    design = fl.BlockDesign()  # OFF first, 20/20, 200 s
    assert design.on_blocks() == [
        (20.0, 20.0),
        (60.0, 20.0),
        (100.0, 20.0),
        (140.0, 20.0),
        (180.0, 20.0),
    ]
    short = fl.BlockDesign(first_block="on", total_s=50.0)
    assert short.on_blocks() == [(0.0, 20.0), (40.0, 10.0)]


def test_task_regressor_rises_after_the_dummy_volumes_with_a_haemodynamic_lag() -> None:
    design = fl.BlockDesign()
    task = fl.task_regressor(design, 260, TR)
    assert task.max() == pytest.approx(1.0)
    first_on = int(round((5 * TR + 20.0) / TR))
    assert task[:first_on].max() < 0.01  # nothing before the first ON block
    assert task[first_on + int(12 / TR)] > 0.95  # at the plateau 12 s in


def test_glm_finds_the_active_voxels_and_percent_change(tmp_path: Path) -> None:
    path = write_run(tmp_path, "1", 251, amplitude=10.0, baseline=500.0, seed=1)
    (run,) = fl.find_runs(tmp_path, "01", "Checkerboard")
    assert run.path == path and run.label == "ses-1"
    result = fl.model_run(run, fl.CHECKERBOARD.design)
    assert result.tr == pytest.approx(TR, abs=1e-4)
    assert result.psc is not None
    roi = np.zeros(SHAPE, bool)
    roi[:, :, :] = True
    stats = fl.roi_stats(result, roi, threshold=fl.DEFAULT_T_THRESHOLD)
    assert stats.peak is not None
    assert all(r.start <= i < r.stop for i, r in zip(stats.peak, ACTIVE, strict=True))
    assert stats.peak_beta == pytest.approx(10.0, rel=0.1)
    assert stats.peak_psc == pytest.approx(2.0, rel=0.1)  # 10 / 500
    assert stats.peak_cnr == pytest.approx(10.0, rel=0.2)  # noise SD 1
    assert stats.n_active >= 8  # all of ACTIVE, few false positives
    assert stats.n_active < 20
    centres, means, _sems, unit = fl.block_average(result, result.voxel_series(stats.peak))  # type: ignore[arg-type]
    assert unit == "% signal change"
    assert centres.size == int(round(40 / TR))
    assert np.nanmax(means) > 1.5  # ~2% during ON, ~0 during OFF
    assert fl.cpac_qc_values(run, "2")["C-PAC mean BOLD SNR"] == "42.5"


def test_demeaned_data_have_no_percent_signal_change(tmp_path: Path) -> None:
    write_run(tmp_path, "1", 251, amplitude=3.0, baseline=0.0, seed=2)
    (run,) = fl.find_runs(tmp_path, "01", "Checkerboard")
    result = fl.model_run(run, fl.CHECKERBOARD.design)
    assert result.psc is None
    stats = fl.roi_stats(result, np.ones(SHAPE, bool), threshold=3.1)
    assert stats.peak_psc is None and stats.peak_t > 10


def test_a_run_shorter_than_the_design_is_refused(tmp_path: Path) -> None:
    write_run(tmp_path, "1", 100, amplitude=3.0, baseline=100.0, seed=3)
    (run,) = fl.find_runs(tmp_path, "01", "Checkerboard")
    with pytest.raises(fl.FirstLevelError, match="shorter than the design"):
        fl.model_run(run, fl.CHECKERBOARD.design)


def test_two_sessions_compare_side_by_side(tmp_path: Path) -> None:
    write_run(tmp_path, "1", 251, amplitude=10.0, baseline=500.0, seed=4)
    write_run(tmp_path, "2", 265, amplitude=5.0, baseline=500.0, seed=5)
    runs = fl.find_runs(tmp_path, "01", "Checkerboard")
    assert [r.label for r in runs] == ["ses-1", "ses-2"]
    assert fl.output_subjects(tmp_path, "Checkerboard") == ["01"]
    results = [fl.model_run(r, fl.CHECKERBOARD.design) for r in runs]
    roi = np.ones(SHAPE, bool)
    stats = [fl.roi_stats(r, roi, 3.1) for r in results]
    rows = dict(fl.comparison_rows(results, stats))
    assert float(rows["Peak CNR"][0]) > float(rows["Peak CNR"][1])
    assert rows["Peak distance to first run (mm)"][0] == "-"
    assert float(rows["Dice of active voxels vs first run"][1]) > 0.6
    assert float(rows["t-map correlation vs first run"][1]) > 0.5
    written = fl.save_maps(results[0], tmp_path / "maps")
    assert len(written) == 4 and all(p.is_file() for p in written)


def test_atlas_labels_split_into_hemispheres_and_resample(tmp_path: Path) -> None:
    xml = tmp_path / "Tiny.xml"
    xml.write_text(
        '<?xml version="1.0"?><atlas><data>'
        '<label index="0" x="1" y="1" z="1">Occipital Pole</label>'
        '<label index="1" x="1" y="1" z="1">Frontal Pole</label>'
        "</data></atlas>"
    )
    assert at.read_labels(xml) == {1: "Occipital Pole", 2: "Frontal Pole"}
    data = np.zeros((4, 2, 2), np.int32)
    data[:, 0, 0] = 1
    data[:, 1, 1] = 2
    affine = np.diag([2.0, 2.0, 2.0, 1.0])
    affine[0, 3] = -4.0  # x = -4, -2, 0, 2: two voxels left, two right
    split, regions = at.split_hemispheres(data, affine, at.read_labels(xml))
    assert split[0, 0, 0] == 1 and split[3, 0, 0] == 1 + at.HEMISPHERE_OFFSET
    assert {r.name for r in regions} >= {"Occipital Pole (L)", "Occipital Pole (R)"}
    assert at.matching_regions(regions, ("occipital",)) == {1, 1 + at.HEMISPHERE_OFFSET}
    coarse = np.diag([4.0, 4.0, 4.0, 1.0])
    coarse[0, 3] = -4.0
    resampled = at.resample_nearest(split, affine, (2, 1, 1), coarse)
    assert resampled[:, 0, 0].tolist() == [1, 1 + at.HEMISPHERE_OFFSET]


def test_fetch_command_copies_every_cached_file_out_of_the_image(tmp_path: Path) -> None:
    command = at.fetch_command(Path("docker"), "img:tag", tmp_path)
    script = command[-1]
    for name in at.cache_files():
        assert name in script
    assert not at.atlases_present(tmp_path)
    for name in at.cache_files():
        (tmp_path / name).write_text("x")
    assert at.atlases_present(tmp_path)
