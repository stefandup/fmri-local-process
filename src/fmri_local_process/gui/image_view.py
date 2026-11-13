"""Zoomable image preview, the same Zoom In / Zoom Out / Fit to Window model as
vr_lab_toolbox's process-results plot tabs (copied in spirit, not imported)."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QResizeEvent, QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

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
