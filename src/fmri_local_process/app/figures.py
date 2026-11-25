"""Figures as PNG: the window shows the bytes, the terminal app the file (inline where the
terminal can, else its path). No pyplot, so any thread may draw."""

import atexit
import io
import re
import shutil
import tempfile
from functools import cache
from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

DPI = 100


def png_bytes(figure: Figure, dpi: int = DPI) -> bytes:
    FigureCanvasAgg(figure)
    buffer = io.BytesIO()
    # facecolor: savefig's own default is white, not the figure's
    figure.savefig(buffer, format="png", dpi=dpi, facecolor=figure.get_facecolor())
    return buffer.getvalue()


@cache
def figures_dir() -> Path:
    """This run's own folder (only the user can read it), removed when the app closes: the
    images show participants' brains, so none are left behind."""
    folder = Path(tempfile.mkdtemp(prefix="fmri_toolbox_"))
    atexit.register(shutil.rmtree, folder, ignore_errors=True)
    return folder


def save_png(data: bytes, name: str) -> Path:
    """The image as a file in this run's figures folder, named after what it shows."""
    path = figures_dir() / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', name)}.png"
    path.write_bytes(data)
    return path
