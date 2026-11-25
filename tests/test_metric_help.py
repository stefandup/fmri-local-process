"""Metric formulas: every equation renders, and every table metric is explained."""

import os

import pytest

from fmri_local_process.processing import first_level as fl
from fmri_local_process.processing.metric_docs import METRIC_DOCS

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.mark.parametrize("tex", [tex for doc in METRIC_DOCS for tex in doc.equations])
def test_every_equation_renders(tex: str) -> None:
    from fmri_local_process.app.metric_help import formula_png

    assert formula_png(tex).startswith(b"\x89PNG")


def test_equations_have_a_transparent_background() -> None:
    """Light text for a dark theme must not land on a white box."""
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QImage

    from fmri_local_process.app.metric_help import formula_png

    image = QImage.fromData(formula_png(r"\dfrac{a}{b}", "#ffffff"))
    assert image.pixelColor(0, 0).alpha() == 0
    assert any(
        image.pixelColor(x, y).alpha() > 200
        for x in range(image.width())
        for y in range(image.height())
    )


def test_every_table_metric_has_a_formula() -> None:
    explained = {row for doc in METRIC_DOCS for row in doc.rows}
    table_metrics = set(fl.METRIC_HELP) | set(fl.MOTION_FIELDS) | {"C-PAC mean BOLD SNR"}
    assert table_metrics <= explained


def test_references_are_links() -> None:
    for doc in METRIC_DOCS:
        for ref in doc.refs:
            assert ref.url.startswith("https://")


def test_dialog_shows_equations_and_links() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from fmri_local_process.gui.metric_help_dialog import MetricHelpDialog

    _app = QApplication.instance() or QApplication([])
    dialog = MetricHelpDialog()
    page = dialog.browser.toHtml()
    assert page.count("formula://") >= sum(len(d.equations) for d in METRIC_DOCS) - 1
    assert "doi.org/10.1016/j.neuroimage.2011.10.018" in page
    assert dialog.browser.openExternalLinks()
