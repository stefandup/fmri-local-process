"""Zoomable image preview, the same Zoom In / Zoom Out / Fit to Window model as
vr_lab_toolbox's process-results plot tabs (copied in spirit, not imported)."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.gui.qt_common import reveal

ZOOM_STEP = 1.25
MIN_SCALE = 0.1
MAX_SCALE = 8.0


class ZoomableImageView(QWidget):
    """One image above a scroll area; fits the width until the user zooms."""

    def __init__(self) -> None:
        super().__init__()
        self.pixmap = QPixmap()
        self.scale = 1.0
        self.fit = True  # refit on resize until the user zooms by hand

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        for text, slot in (
            ("Zoom In", self.zoom_in),
            ("Zoom Out", self.zoom_out),
            ("Fit to Window", self.fit_to_window),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll_area = QScrollArea()
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll_area.setWidget(self.image_label)
        layout.addWidget(self.scroll_area, 1)

    def set_pixmap(self, pixmap: QPixmap, placeholder: str = "") -> None:
        self.pixmap = pixmap
        if pixmap.isNull():
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(placeholder)
            self.image_label.adjustSize()
            return
        self.image_label.setText("")
        self.fit_to_window()

    def apply_scale(self) -> None:
        if self.pixmap.isNull():
            return
        scaled = self.pixmap.scaled(
            max(1, round(self.pixmap.width() * self.scale)),
            max(1, round(self.pixmap.height() * self.scale)),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)
        self.image_label.resize(scaled.size())

    def zoom_in(self) -> None:
        self.fit = False
        self.scale = min(self.scale * ZOOM_STEP, MAX_SCALE)
        self.apply_scale()

    def zoom_out(self) -> None:
        self.fit = False
        self.scale = max(self.scale / ZOOM_STEP, MIN_SCALE)
        self.apply_scale()

    def fit_to_window(self) -> None:
        """Fit both width and height: C-PAC's QC figures are single plots, not tall
        multi-panel dashboards, so the whole figure should be visible at once."""
        self.fit = True
        viewport = self.scroll_area.viewport().size()
        if self.pixmap.isNull() or viewport.width() <= 1 or viewport.height() <= 1:
            return
        self.scale = min(
            viewport.width() / self.pixmap.width(), viewport.height() / self.pixmap.height()
        )
        self.apply_scale()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 -- Qt override
        # A hidden tab's viewport has no real size yet; fit once it's laid out.
        super().showEvent(event)
        if self.fit:
            QTimer.singleShot(0, self.fit_to_window)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 -- Qt override
        super().resizeEvent(event)
        if self.fit:
            self.fit_to_window()


def figure_to_pixmap(figure: Figure, dpi: int = 100) -> QPixmap:
    """Rasterise a matplotlib Figure in memory (no file, no pyplot)."""
    figure.set_dpi(dpi)
    canvas = FigureCanvasAgg(figure)
    canvas.draw()
    width, height = canvas.get_width_height()
    image = QImage(canvas.buffer_rgba(), width, height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(image.copy())  # copy: the buffer dies with the canvas


class Previewable(Protocol):
    @property
    def label(self) -> str: ...
    @property
    def path(self) -> Path: ...


def load_figure(item: Previewable) -> QPixmap:
    """A preview that is already an image file (e.g. a C-PAC QC PNG)."""
    return QPixmap(str(item.path))


class PreviewPicker(QWidget):
    """Dropdown + Prev/Next/Reveal over a list of previews, above a ZoomableImageView.

    load(item) gives the image, or a message saying why there is none. Loading waits
    until the picker is on screen, so a slow loader costs nothing while its tab is hidden.
    """

    def __init__(self, load: Callable[[Any], QPixmap | str], empty_text: str, tooltip: str) -> None:
        super().__init__()
        self.load = load
        self.empty_text = empty_text
        self.items: list[Previewable] = []
        self.stale = False  # the current item changed while hidden
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.setToolTip(tooltip)
        self.combo.currentIndexChanged.connect(self.show_current)
        row.addWidget(self.combo, 1)
        self.buttons: list[QPushButton] = []
        for text, slot in (
            ("◀ Prev", lambda: self.step(-1)),
            ("Next ▶", lambda: self.step(1)),
            ("Reveal", self.reveal_current),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
            self.buttons.append(button)
        layout.addLayout(row)
        self.view = ZoomableImageView()
        layout.addWidget(self.view, 1)

    def set_items(self, items: Sequence[Previewable]) -> None:
        # Stay on the same kind of preview when switching subjects.
        previous = self.combo.currentText()
        self.items = list(items)
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems([item.label for item in self.items])
        self.combo.setCurrentIndex(max(self.combo.findText(previous), 0))
        self.combo.blockSignals(False)
        for button in self.buttons:
            button.setEnabled(bool(self.items))
        self.show_current()

    def current(self) -> Previewable | None:
        index = self.combo.currentIndex()
        return self.items[index] if 0 <= index < len(self.items) else None

    def show_current(self) -> None:
        item = self.current()
        if item is None:
            self.view.set_pixmap(QPixmap(), self.empty_text)
            return
        self.combo.setToolTip(str(item.path))
        if not self.isVisible():
            self.stale = True
            return
        self.stale = False
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            loaded = self.load(item)
        finally:
            QApplication.restoreOverrideCursor()
        if isinstance(loaded, str):
            self.view.set_pixmap(QPixmap(), loaded)
        else:
            self.view.set_pixmap(loaded, f"Couldn't load {item.path.name}.")

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 -- Qt override
        super().showEvent(event)
        if self.stale:
            self.show_current()

    def step(self, step: int) -> None:
        if self.items:
            self.combo.setCurrentIndex((self.combo.currentIndex() + step) % len(self.items))

    def reveal_current(self) -> None:
        item = self.current()
        if item is not None:
            reveal(item.path.parent)
