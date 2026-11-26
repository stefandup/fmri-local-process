"""Small PySide6 style helpers, matching vr_lab_toolbox's gui/qt_common.py look.

Kept to what this project's GUIs use today; values are copied (not imported) so this
package doesn't depend on vr_lab_toolbox.
"""

import textwrap
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QLabel, QSizePolicy

from fmri_local_process.app.colors import WARNING_COLOR

SETTINGS_ORGANIZATION = "MooiToolbox"  # same as vr_lab_toolbox
TOOLTIP_WRAP_WIDTH = 68
SECONDARY_TEXT_COLOR = "#9aa0a6"
NAME_LABEL_FONT_POINT_INCREASE = 9
PRIMARY_BUTTON_DISABLED_BG = "#555555"
PRIMARY_BUTTON_DISABLED_FG = "#999999"
HOME_BASE_ACCENT_COLOR = "#5b9bd5"
HOME_BASE_NAME_EXTRA_POINT_INCREASE = 2
# Border of the raw-data panels (read only, the converter's input), beside the blue BIDS ones.
RAW_ACCENT_COLOR = "#27ae60"
# "Run the pipeline" accent, the same teal as vr_lab_toolbox's "Process BIDS Folder".
PROCESS_ACCENT_COLOR = "#16a085"
PROCESS_ACCENT_HOVER_COLOR = "#128f76"


NOTICE_BACKGROUND = "#fdebd0"  # light amber, under the WARNING_COLOR edge


class NoticeBanner(QLabel):
    """What a tab is missing to show or do its work, said plainly with where to fix it, e.g.
    'No behavioural logs found in X'. One line per problem; hidden when nothing is missing."""

    def __init__(self) -> None:
        super().__init__()
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setStyleSheet(
            f"QLabel {{ background-color: {NOTICE_BACKGROUND}; color: black; "
            f"border-left: 5px solid {WARNING_COLOR}; padding: 6px 10px; font-weight: bold; }}"
        )
        self.shown: list[str] = []
        self.setVisible(False)

    def set_messages(self, messages: list[str]) -> None:
        self.shown = list(messages)
        self.setText("\n".join(f"⚠ {message}" for message in messages))
        self.setVisible(bool(messages))


def apply_app_style(app: QApplication) -> None:
    """vr_lab_toolbox's look: Qt's Fusion style (crisper contrast than Windows' native one,
    and it follows the stylesheets fully) and black-on-white tooltips."""
    app.setStyle("Fusion")
    app.setStyleSheet(
        app.styleSheet() + "QToolTip { color: black; background-color: white; "
        "border: 1px solid black; }"
    )


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


def shrinkable(label: QLabel) -> QLabel:
    """Let a label with a long unbreakable text (a path) shrink with the window instead of
    widening it; the whole text stays in its tooltip."""
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    label.setMinimumWidth(1)
    return label


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
