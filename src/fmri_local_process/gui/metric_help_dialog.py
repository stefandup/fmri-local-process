"""Metric formulas: every metric of a tab's table with its equation (rendered by matplotlib's
mathtext in Computer Modern, so no LaTeX install is needed), what it means, typical values
where known, and references that open in the browser. Used by First-level stats and MRIQC."""

from collections.abc import Sequence

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage, QPalette, QTextDocument
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout, QWidget

from fmri_local_process.app.metric_help import (
    FIRST_LEVEL_HEADING,
    FORMULA_DPI,
    formula_png,
    help_html,
)
from fmri_local_process.processing.metric_docs import METRIC_DOCS, MetricDoc


class MetricHelpDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        docs: Sequence[MetricDoc] = METRIC_DOCS,
        heading: str = FIRST_LEVEL_HEADING,
        title: str = "Metric formulas",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 760)
        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)  # references open in the web browser
        text_color = self.palette().color(QPalette.ColorRole.Text).name()
        images: dict[str, str] = {}
        document = self.browser.document()
        for doc in docs:
            for tex in doc.equations:
                if tex in images:
                    continue
                url = f"formula://{len(images)}.png"
                image = QImage.fromData(formula_png(tex, text_color))
                # Rendered at FORMULA_DPI; show it at screen size, sharp on high-DPI screens.
                image.setDevicePixelRatio(FORMULA_DPI / 96)
                document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(url), image)
                images[tex] = url
        self.browser.setHtml(help_html(images, docs, heading))
        layout.addWidget(self.browser)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
