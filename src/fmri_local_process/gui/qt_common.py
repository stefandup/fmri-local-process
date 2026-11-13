"""Small PySide6 style helpers, matching vr_lab_toolbox's gui/qt_common.py look.

Kept to what this project's GUIs use today; values are copied (not imported) so this
package doesn't depend on vr_lab_toolbox.
"""

import textwrap
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel

SETTINGS_ORGANIZATION = "MooiToolbox"  # same as vr_lab_toolbox
TOOLTIP_WRAP_WIDTH = 68
SECONDARY_TEXT_COLOR = "#9aa0a6"
NAME_LABEL_FONT_POINT_INCREASE = 9
PRIMARY_BUTTON_DISABLED_BG = "#555555"
PRIMARY_BUTTON_DISABLED_FG = "#999999"
HOME_BASE_ACCENT_COLOR = "#5b9bd5"
HOME_BASE_NAME_EXTRA_POINT_INCREASE = 2
# "Run the pipeline" accent, the same teal as vr_lab_toolbox's "Process BIDS Folder".
PROCESS_ACCENT_COLOR = "#16a085"
PROCESS_ACCENT_HOVER_COLOR = "#128f76"

OK_COLOR = "#2ecc71"
RUNNING_COLOR = "#3498db"
WARNING_COLOR = "#f39c12"
ERROR_COLOR = "#e74c3c"
UNKNOWN_COLOR = "#9aa0a6"


def default_browse_dir(preferred: Path | None = None) -> str:
    """Start folder for a folder picker: preferred if it exists, else the home folder."""
    if preferred is not None and preferred.is_dir():
        return str(preferred)
    return QStandardPaths.writableLocation(QStandardPaths.StandardLocation.HomeLocation)


def wrap_tooltip(text: str, width: int = TOOLTIP_WRAP_WIDTH) -> str:
    """Hard-wrap each paragraph; Qt's plain-text tooltips never wrap on their own."""
    return "\n\n".join(
        paragraph if "\n" in paragraph else "\n".join(textwrap.wrap(paragraph, width=width))
        for paragraph in text.split("\n\n")
    )


def style_secondary_label(label: QLabel) -> None:
    label.setStyleSheet(f"color: {SECONDARY_TEXT_COLOR};")


def style_name_label(label: QLabel, extra_points: int = 0) -> None:
    font = label.font()
    font.setPointSize(font.pointSize() + NAME_LABEL_FONT_POINT_INCREASE + extra_points)
    font.setBold(True)
    label.setFont(font)
    label.setWordWrap(True)


def set_path_display(path_label: QLabel, name_label: QLabel, path: Path) -> None:
    """Small 'Path: <parent>' caption above a large bold folder name."""
    path_label.setText(f"Path: {path.parent}")
    name_label.setText(path.name or str(path))


def primary_action_stylesheet(base_color: str, hover_color: str) -> str:
    return (
        f"QPushButton {{ font-weight: bold; font-size: 13px; padding: 6px 20px; "
        f"background-color: {base_color}; color: white; border-radius: 4px; border: none; }}"
        f"QPushButton:hover {{ background-color: {hover_color}; }}"
        f"QPushButton:disabled {{ background-color: {PRIMARY_BUTTON_DISABLED_BG}; "
        f"color: {PRIMARY_BUTTON_DISABLED_FG}; }}"
    )


def accent_group_box_stylesheet(color: str) -> str:
    return (
        f"QGroupBox {{ border: 2px solid {color}; border-radius: 4px; margin-top: 8px; }}"
        "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
    )


def reveal(folder: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def paths_conflict(first: Path, second: Path) -> bool:
    """True when the folders are the same or one contains the other."""
    a, b = first.resolve(), second.resolve()
    return a == b or a in b.parents or b in a.parents
