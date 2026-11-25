"""The window's shared state: app/state.py with its choices remembered in QSettings (the
registry on Windows; in a review package, a file in the package)."""

from PySide6.QtCore import QSettings

from fmri_local_process.app.state import ToolboxState as AppState
from fmri_local_process.gui.qt_common import SETTINGS_ORGANIZATION
from fmri_local_process.processing.review_mode import package_root, settings_dir


def make_settings(app: str) -> QSettings:
    """The settings store for app (a function, so tests can point it at a temp file). In a
    review package it is a file in the package, so the reviewer's choices stay with it."""
    if (root := package_root()) is not None:
        return QSettings(str(settings_dir(root) / f"{app}.ini"), QSettings.Format.IniFormat)
    return QSettings(SETTINGS_ORGANIZATION, app)


class ToolboxState(AppState):
    def __init__(self) -> None:
        super().__init__(lambda app: make_settings(app))  # looked up now: tests replace it
