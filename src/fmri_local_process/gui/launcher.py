"""Start the toolbox with a splash screen while it loads.

Only Qt is imported here, so the splash shows within a fraction of a second; the toolbox
itself (about 1-2 s of imports, then building the tabs) is loaded behind it. The splash
image is gui/assets/splash.png when that file exists (any size; about 640 x 360 suits), else
a plain one drawn here. Status messages appear at its bottom left.

The entry points: `fmri_toolbox`, and `fmri_crosscheck` / `fmri_preprocess_gui`, which open
the toolbox on their tab.
"""

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

SPLASH_IMAGE = Path(__file__).parent / "assets" / "splash.png"
SPLASH_SIZE = (640, 360)
# The drawn placeholder: the toolbox's blue (BIDS) into the raw-data green.
TOP_COLOR = "#1f3a5f"
BOTTOM_COLOR = "#1e5b4a"
TEXT_COLOR = "#ffffff"
MUTED_TEXT_COLOR = "#c9d6e3"
TITLE_POINT_SIZE = 26
SUBTITLE_POINT_SIZE = 11
MARGIN = 32


def app_version() -> str:
    try:
        return version("fmri-local-process").split("+")[0]  # without the git build tag
    except PackageNotFoundError:
        return ""


def placeholder_pixmap() -> QPixmap:
    """A plain splash until a designed one is saved as assets/splash.png."""
    width, height = SPLASH_SIZE
    pixmap = QPixmap(width, height)
    painter = QPainter(pixmap)
    gradient = QLinearGradient(0, 0, 0, height)
    gradient.setColorAt(0, QColor(TOP_COLOR))
    gradient.setColorAt(1, QColor(BOTTOM_COLOR))
    painter.fillRect(pixmap.rect(), gradient)
    painter.setPen(QColor(TEXT_COLOR))
    title_font = QFont()
    title_font.setPointSize(TITLE_POINT_SIZE)
    title_font.setBold(True)
    painter.setFont(title_font)
    title_area = QRect(MARGIN, MARGIN, width - 2 * MARGIN, height // 3)
    painter.drawText(title_area, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                     "Philani fMRI toolbox")  # fmt: skip
    subtitle_font = QFont()
    subtitle_font.setPointSize(SUBTITLE_POINT_SIZE)
    painter.setFont(subtitle_font)
    painter.setPen(QColor(MUTED_TEXT_COLOR))
    lines = ["DICOM ➜ BIDS  ·  crosscheck  ·  C-PAC preprocessing"]
    if number := app_version():
        lines.append(f"version {number}")
    subtitle_area = QRect(MARGIN, MARGIN + height // 3 + 8, width - 2 * MARGIN, height // 3)
    painter.drawText(subtitle_area, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                     "\n".join(lines))  # fmt: skip
    painter.end()
    return pixmap


def splash_pixmap() -> QPixmap:
    if SPLASH_IMAGE.is_file():
        image = QPixmap(str(SPLASH_IMAGE))
        if not image.isNull():
            return image
    return placeholder_pixmap()


def show_message(splash: QSplashScreen, text: str) -> None:
    splash.showMessage(
        text, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, QColor(TEXT_COLOR)
    )
    QApplication.processEvents()  # paint it now: the next step blocks for a moment


def launch(start_tab: str | None = None) -> None:
    app = QApplication(sys.argv)
    splash = QSplashScreen(splash_pixmap())
    splash.show()
    show_message(splash, "Loading the tools...")

    from fmri_local_process.gui import toolbox  # the slow imports, behind the splash

    show_message(splash, "Opening the tabs...")
    window = toolbox.start(app, start_tab or toolbox.SETUP)
    splash.finish(window)  # closes once the window is up
    sys.exit(app.exec())


def main() -> None:
    """fmri_toolbox"""
    launch()


def crosscheck() -> None:
    """fmri_crosscheck: the toolbox on its BIDS Crosscheck tab."""
    launch("BIDS Crosscheck")


def preprocess() -> None:
    """fmri_preprocess_gui: the toolbox on its Preprocess BIDS (C-PAC) tab."""
    launch("Preprocess BIDS (C-PAC)")
