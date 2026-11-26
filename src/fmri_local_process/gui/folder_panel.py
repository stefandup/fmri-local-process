"""A folder panel in vr_lab_toolbox's style: a small "Path:" caption, the folder's name large
and bold, and Browse / Reveal. The BIDS folder is the "home base" panel (accent border, a
larger name). The folder lives in the toolbox's shared state, so every panel showing it
follows a change made on any tab."""

from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from fmri_local_process.app.state import folder_conflict, folder_locked
from fmri_local_process.gui.qt_common import (
    HOME_BASE_ACCENT_COLOR,
    HOME_BASE_NAME_EXTRA_POINT_INCREASE,
    accent_group_box_stylesheet,
    default_browse_dir,
    reveal,
    shrinkable,
    style_name_label,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.gui.toolbox_state import ToolboxState


class FolderPanel(QGroupBox):
    def __init__(
        self,
        state: ToolboxState,
        key: str,
        title: str,
        tooltip: str,
        *,
        home_base: bool = False,
        can_browse: bool = True,
        border_color: str | None = None,
    ) -> None:
        super().__init__(title)
        self.state, self.key, self.title_text = state, key, title
        self.setToolTip(wrap_tooltip(tooltip))
        if border_color is None and home_base:
            border_color = HOME_BASE_ACCENT_COLOR
        if border_color is not None:
            self.setStyleSheet(accent_group_box_stylesheet(border_color))
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        self.path_label = shrinkable(QLabel(""))
        style_secondary_label(self.path_label)
        layout.addWidget(self.path_label)
        self.name_label = shrinkable(QLabel(""))
        style_name_label(self.name_label, HOME_BASE_NAME_EXTRA_POINT_INCREASE if home_base else 0)
        self.name_label.setWordWrap(False)
        layout.addWidget(self.name_label)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(6)
        self.browse_button: QPushButton | None = None
        if can_browse:
            self.browse_button = QPushButton("Browse...")
            self.browse_button.setToolTip(wrap_tooltip(f"Pick the {title.lower()}."))
            self.browse_button.clicked.connect(self.on_browse)
            self.buttons.addWidget(self.browse_button)
        self.reveal_button = QPushButton("Reveal")
        self.reveal_button.setToolTip(
            wrap_tooltip("Open the folder in the file browser, to look at what's on disk.")
        )
        self.reveal_button.clicked.connect(self.on_reveal)
        self.buttons.addWidget(self.reveal_button)
        self.buttons.addStretch(1)
        layout.addLayout(self.buttons)
        state.folder_changed.connect(self.on_folder_changed)
        state.busy_changed.connect(self.refresh)
        self.refresh()

    def on_folder_changed(self, key: str) -> None:
        if key == self.key:
            self.refresh()

    def refresh(self) -> None:
        folder = self.state.folder(self.key)
        if folder is None:
            self.path_label.setText("")
            self.name_label.setText("Not chosen yet")
        else:
            self.path_label.setText(f"Path: {folder.parent}\\")
            self.name_label.setText(folder.name or str(folder))
        self.path_label.setToolTip(str(folder) if folder else "")
        self.name_label.setToolTip(str(folder) if folder else "")
        self.reveal_button.setEnabled(folder is not None)
        if self.browse_button is not None:
            self.browse_button.setEnabled(not folder_locked(self.state, self.key))

    def on_browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, f"Choose: {self.title_text}", default_browse_dir(self.state.folder(self.key))
        )
        if not chosen:
            return
        folder = Path(chosen)
        if why := folder_conflict(self.state, self.key, folder):
            QMessageBox.warning(self, "Pick another folder", f"Not that folder: {why}.")
            return
        self.state.set_folder(self.key, folder)

    def on_reveal(self) -> None:
        folder = self.state.folder(self.key)
        if folder is not None:
            reveal(folder)
