"""C-PAC output discovery for the preview tabs, on a synthetic output tree."""

from pathlib import Path

from fmri_local_process.processing.cpac_outputs import (
    bids_entities,
    motion_summaries,
    qc_figures,
    read_tsv,
)

FUNC = "output/pipeline_cpac_preproc/sub-P01/ses-{ses}/func"


def make_outputs(out: Path) -> None:
    for ses, name in [
        ("1", "sub-P01_ses-1_task-Rest_desc-movementParametersRot_quality.png"),
        ("1", "sub-P01_ses-1_task-Rest_reg-2_desc-boldSnrAxial_quality.png"),
        ("1", "sub-P01_ses-1_task-Rest_desc-preproc_bold.nii.gz"),
        ("2", "sub-P01_ses-2_task-Matching_desc-framewiseDisplacementJenkinsonPlot_quality.png"),
        ("2", "sub-P01_ses-2_task-Matching_desc-summary_motion.tsv"),
    ]:
        folder = out / FUNC.format(ses=ses)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).touch()
    other = out / "output/pipeline_cpac_preproc/sub-P010/ses-1/func"  # prefix of P01
    other.mkdir(parents=True)
    (other / "sub-P010_ses-1_task-Rest_desc-movementParametersRot_quality.png").touch()


def test_bids_entities() -> None:
    name = "sub-P01_ses-2_task-Rest_reg-1_desc-boldSnr_quality.png"
    assert bids_entities(name) == {
        "sub": "P01",
        "ses": "2",
        "task": "Rest",
        "reg": "1",
        "desc": "boldSnr",
    }


def test_qc_figures_labels_and_task_filter(tmp_path: Path) -> None:
    make_outputs(tmp_path)
    labels = [figure.label for figure in qc_figures(tmp_path, "P01")]
    assert labels == [
        "ses-1 Rest: Motion: rotation",
        "ses-1 Rest: BOLD SNR (axial) (reg-2)",
        "ses-2 Matching: Framewise displacement (Jenkinson)",
    ]
    rest = qc_figures(tmp_path, "P01", "Rest")
    assert [figure.desc for figure in rest] == ["movementParametersRot", "boldSnrAxial"]
    assert qc_figures(tmp_path, "P99") == []


def test_motion_summaries_and_read_tsv(tmp_path: Path) -> None:
    make_outputs(tmp_path)
    [tsv] = motion_summaries(tmp_path, "P01")
    assert motion_summaries(tmp_path, "P01", "Rest") == []
    assert read_tsv(tsv) == ([], [])
    tsv.write_text("MeanFD\tMaxFD\n0.12\t0.9\n")
    assert read_tsv(tsv) == (["MeanFD", "MaxFD"], [["0.12", "0.9"]])
