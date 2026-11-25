"""The MRIQC tab's Metric formulas: every table column explained, every equation renders."""

import os

import pytest

from fmri_local_process.processing import mriqc as mq
from fmri_local_process.processing.mriqc_docs import MRIQC_DOCS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ALL_DOCS = [doc for docs in MRIQC_DOCS.values() for doc in docs]


@pytest.mark.parametrize("tex", sorted({tex for doc in ALL_DOCS for tex in doc.equations}))
def test_every_equation_renders(tex: str) -> None:
    from fmri_local_process.app.metric_help import formula_png

    assert formula_png(tex).startswith(b"\x89PNG")


@pytest.mark.parametrize("modality", mq.MODALITIES)
def test_every_table_column_has_a_formula(modality: str) -> None:
    explained = {row for doc in MRIQC_DOCS[modality] for row in doc.rows}
    assert {metric.label for metric in mq.METRICS[modality]} <= explained


def test_references_are_links() -> None:
    assert all(ref.url.startswith("https://") for doc in ALL_DOCS for ref in doc.refs)


def test_dialog_shows_typical_values_and_references() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from fmri_local_process.gui.metric_help_dialog import MetricHelpDialog

    _app = QApplication.instance() or QApplication([])
    dialog = MetricHelpDialog(None, MRIQC_DOCS["bold"], "MRIQC BOLD", "MRIQC formulas")
    page = dialog.browser.toHtml()
    assert dialog.windowTitle() == "MRIQC formulas"
    assert "Typical values:" in page
    assert "0.2 mm" in page  # the FD rule of thumb
    assert "doi.org/10.1016/j.neuroimage.2017.12.073" in page  # Parkes 2018
