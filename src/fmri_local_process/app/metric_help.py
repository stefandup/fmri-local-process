"""Metric formulas: every metric of a tab's table with its equation (rendered by matplotlib's
mathtext in Computer Modern, so no LaTeX install is needed), what it means, typical values
where known, and references."""

import html
import io
from collections.abc import Sequence

import matplotlib
from matplotlib import mathtext
from matplotlib.font_manager import FontProperties

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


FIRST_LEVEL_HEADING = "How the First-level stats metrics are calculated"


def help_html(
    images: dict[str, str],
    docs: Sequence[MetricDoc] = METRIC_DOCS,
    heading: str = FIRST_LEVEL_HEADING,
) -> str:
    """The page; images maps each equation to the resource URL its PNG is under."""
    contents = " · ".join(f'<a href="#{anchor(doc)}">{html.escape(doc.title)}</a>' for doc in docs)
    parts = [f"<h2>{html.escape(heading)}</h2>", f"<p>{contents}</p>"]
    for doc in docs:
        parts.append(f'<h3><a name="{anchor(doc)}"></a>{html.escape(doc.title)}</h3>')
        if doc.rows:
            rows = ", ".join(f"<i>{html.escape(r)}</i>" for r in doc.rows)
            parts.append(f"<p><small>Table rows: {rows}</small></p>")
        for tex in doc.equations:
            parts.append(f'<p style="margin-left:24px"><img src="{images[tex]}"></p>')
        parts.append(f"<p>{html.escape(doc.text)}</p>")
        if doc.typical:
            parts.append(f"<p><b>Typical values:</b> {html.escape(doc.typical)}</p>")
        if doc.refs:
            refs = "".join(
                f'<li>{html.escape(ref.text)} <a href="{ref.url}">{html.escape(ref.url)}</a></li>'
                for ref in doc.refs
            )
            parts.append(f"<ul>{refs}</ul>")
    return "\n".join(parts)


def help_lines(docs: Sequence[MetricDoc]) -> list[str]:
    """The same page as plain text, the equations in their TeX spelling."""
    lines = []
    for doc in docs:
        lines += [doc.title, "-" * len(doc.title)]
        if doc.rows:
            lines.append(f"Table rows: {', '.join(doc.rows)}")
        lines += [f"    {tex}" for tex in doc.equations]
        lines.append(doc.text)
        if doc.typical:
            lines.append(f"Typical values: {doc.typical}")
        lines += [f"  - {ref.text} {ref.url}" for ref in doc.refs]
        lines.append("")
    return lines
