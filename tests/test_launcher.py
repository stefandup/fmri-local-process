"""Offscreen tests of the splash screen the toolbox shows while it loads."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPixmap  # noqa: E402 -- needs QT_QPA_PLATFORM set first
from PySide6.QtWidgets import QApplication  # noqa: E402

from fmri_local_process.gui import launcher  # noqa: E402


def test_placeholder_is_drawn_until_a_designed_image_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(launcher, "SPLASH_IMAGE", tmp_path / "splash.png")  # not there yet
    drawn = launcher.splash_pixmap()
    assert (drawn.width(), drawn.height()) == launcher.SPLASH_SIZE

    designed = QPixmap(800, 450)
    designed.fill(QColor("red"))
    designed.save(str(tmp_path / "splash.png"))
    assert (launcher.splash_pixmap().width(), launcher.splash_pixmap().height()) == (800, 450)


def test_launcher_tab_names_are_the_toolbox_tabs() -> None:
    from fmri_local_process.gui import toolbox  # the launcher avoids importing this early

    assert toolbox.CROSSCHECK == "BIDS Crosscheck"
    assert toolbox.PREPROCESS == "Preprocess BIDS (C-PAC)"
    assert {toolbox.CROSSCHECK, toolbox.PREPROCESS} <= set(toolbox.TAB_ORDER)
