"""Metric formulas: every First-level stats metric with its equation (rendered by matplotlib's
mathtext in Computer Modern, so no LaTeX install is needed), what it means, and references
that open in the browser."""

import html
import io

import matplotlib
from matplotlib import mathtext
from matplotlib.font_manager import FontProperties
from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage, QPalette, QTextDocument
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout, QWidget

from fmri_local_process.processing.metric_docs import METRIC_DOCS, MetricDoc

FORMULA_DPI = 200
FORMULA_POINTS = 15


def formula_png(tex: str, color: str = "black", dpi: int = FORMULA_DPI) -> bytes:
    """One equation as a PNG with a transparent background."""
    buffer = io.BytesIO()
    # Transparent: with a white background, a dark theme's light text is invisible.
    with matplotlib.rc_context({"mathtext.fontset": "cm", "savefig.transparent": True}):
        mathtext.math_to_image(
            f"${tex}$", buffer, FontProperties(size=FORMULA_POINTS), dpi, "png", color=color
        )
    return buffer.getvalue()


def anchor(doc: MetricDoc) -> str:
    return "m-" + "".join(c if c.isalnum() else "-" for c in doc.title.lower())


def help_html(images: dict[str, str]) -> str:
    """The page; images maps each equation to the resource URL its PNG is under."""
    contents = " · ".join(
        f'<a href="#{anchor(doc)}">{html.escape(doc.title)}</a>' for doc in METRIC_DOCS
    )
    parts = [
        "<h2>How the First-level stats metrics are calculated</h2>",
        f"<p>{contents}</p>",
    ]
    for doc in METRIC_DOCS:
        parts.append(f'<h3><a name="{anchor(doc)}"></a>{html.escape(doc.title)}</h3>')
        if doc.rows:
            rows = ", ".join(f"<i>{html.escape(r)}</i>" for r in doc.rows)
            parts.append(f"<p><small>Table rows: {rows}</small></p>")
        for tex in doc.equations:
            parts.append(f'<p style="margin-left:24px"><img src="{images[tex]}"></p>')
        parts.append(f"<p>{html.escape(doc.text)}</p>")
        if doc.refs:
            refs = "".join(
                f'<li>{html.escape(ref.text)} <a href="{ref.url}">{html.escape(ref.url)}</a></li>'
                for ref in doc.refs
            )
            parts.append(f"<ul>{refs}</ul>")
    return "\n".join(parts)


class MetricHelpDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Metric formulas")
        self.resize(820, 760)
        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)  # references open in the web browser
        text_color = self.palette().color(QPalette.ColorRole.Text).name()
        images: dict[str, str] = {}
        document = self.browser.document()
        for doc in METRIC_DOCS:
            for tex in doc.equations:
                if tex in images:
                    continue
                url = f"formula://{len(images)}.png"
                image = QImage.fromData(formula_png(tex, text_color))
                # Rendered at FORMULA_DPI; show it at screen size, sharp on high-DPI screens.
                image.setDevicePixelRatio(FORMULA_DPI / 96)
                document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(url), image)
                images[tex] = url
        self.browser.setHtml(help_html(images))
        layout.addWidget(self.browser)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
