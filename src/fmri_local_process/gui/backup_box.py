"""The "Crosscheck backup" box on the Setup & Convert to BIDS tab (vr_lab_toolbox's Save /
Restore Crosscheck Data): the study's ID, Save now, auto-save every few minutes, and
Restore. The saving itself is processing/crosscheck_backup.py; restoring needs a
conversion, so the box only asks for it (`restore_requested`) and the tab does it."""

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from fmri_local_process.app.backup import (
    BACKUP_FOLDER,
    DEFAULT_AUTOSAVE_MINUTES,
    DEFAULT_STUDY_ID,
    MAX_AUTOSAVE_MINUTES,
    NOTHING_TO_SAVE,
    no_backup_text,
    safe_name,
    saved_message,
)
from fmri_local_process.gui.qt_common import (
    HOME_BASE_ACCENT_COLOR,
    accent_group_box_stylesheet,
    reveal,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.gui.toolbox_state import ToolboxState
from fmri_local_process.processing.crosscheck_backup import list_backups, save_backup


def backup_base() -> Path:
    """Where every study's backups live: the user's local app-data folder, outside any BIDS
    folder (a function, so tests can point it at a temp folder)."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    return Path(base).joinpath(*BACKUP_FOLDER)


class BackupBox(QGroupBox):
    restore_requested = Signal(object)  # Path of the saved copy to restore
    message = Signal(str)  # for the tab's activity log

    def __init__(self, state: ToolboxState) -> None:
        super().__init__("Crosscheck backup")
        self.state = state
        self.setStyleSheet(accent_group_box_stylesheet(HOME_BASE_ACCENT_COLOR))
        self.setToolTip(
            wrap_tooltip(
                "Copies of every crosscheck decision (ID and log corrections, deleted "
                "sessions, notes, settings, expected lengths, kept runs, trims and the action "
                "log), kept outside the BIDS folder under the study ID, so they survive the "
                "BIDS folder being lost. Restore rebuilds a BIDS folder from raw with them."
            )
        )
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Study ID:"))
        self.study_edit = QLineEdit(str(state.settings.value("study_id", DEFAULT_STUDY_ID)))
        self.study_edit.setToolTip(
            wrap_tooltip("Names the backup folder. Keep it the same for one study.")
        )
        self.study_edit.editingFinished.connect(self.on_study_changed)
        row.addWidget(self.study_edit, 1)
        save = QPushButton("Save now")
        save.setToolTip("Save a backup of the crosscheck decisions now.")
        save.clicked.connect(lambda: self.save(manual=True))
        row.addWidget(save)
        self.restore_button = QPushButton("Restore...")
        self.restore_button.setToolTip(
            wrap_tooltip(
                "Disaster recovery: put a saved copy's decisions back into the BIDS folder, "
                "convert again from raw (which applies the corrections and deleted "
                "sessions), then replay the kept runs and trims. For an empty or freshly "
                "made BIDS folder, not to merge into one with its own changes."
            )
        )
        self.restore_button.clicked.connect(self.on_restore)
        row.addWidget(self.restore_button)
        layout.addLayout(row)

        auto_row = QHBoxLayout()
        self.autosave_box = QCheckBox("Auto-save every")
        self.autosave_box.setChecked(
            str(state.settings.value("autosave", "true")).lower() == "true"
        )
        self.autosave_box.toggled.connect(self.on_autosave_changed)
        auto_row.addWidget(self.autosave_box)
        self.minutes_spin = QSpinBox()
        self.minutes_spin.setRange(1, MAX_AUTOSAVE_MINUTES)
        self.minutes_spin.setSuffix(" min")
        self.minutes_spin.setValue(
            int(str(state.settings.value("autosave_minutes", DEFAULT_AUTOSAVE_MINUTES)))
        )
        self.minutes_spin.valueChanged.connect(self.on_autosave_changed)
        auto_row.addWidget(self.minutes_spin)
        self.status = QLabel()
        style_secondary_label(self.status)
        auto_row.addWidget(self.status, 1)
        reveal_button = QPushButton("Reveal")
        reveal_button.setToolTip("Open the study's backup folder.")
        reveal_button.clicked.connect(self.on_reveal)
        auto_row.addWidget(reveal_button)
        layout.addLayout(auto_row)

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.save(manual=False))
        self.on_autosave_changed()

    def study_id(self) -> str:
        return safe_name(self.study_edit.text())

    def folder(self) -> Path:
        return backup_base() / self.study_id()

    def on_study_changed(self) -> None:
        self.state.settings.setValue("study_id", self.study_edit.text().strip())

    def on_autosave_changed(self) -> None:
        self.state.settings.setValue("autosave", self.autosave_box.isChecked())
        self.state.settings.setValue("autosave_minutes", self.minutes_spin.value())
        self.timer.stop()
        if self.autosave_box.isChecked():
            self.timer.start(self.minutes_spin.value() * 60_000)

    def save(self, manual: bool) -> list[Path]:
        bids_root = self.state.folder("bids")
        if bids_root is None:
            return []
        try:
            saved = save_backup(bids_root, self.folder())
        except OSError as e:
            self.message.emit(f"ERROR  backup not saved: {e}")
            if manual:
                QMessageBox.warning(self, "Backup not saved", str(e))
            return []
        if saved:
            self.status.setText(f"Saved {len(saved)} file(s) at {self.time_now()}")
            self.message.emit(saved_message(len(saved), self.folder()))
        elif manual:
            self.message.emit(NOTHING_TO_SAVE)
        return saved

    @staticmethod
    def time_now() -> str:
        return datetime.now().strftime("%H:%M")

    def on_reveal(self) -> None:
        folder = self.folder()
        folder.mkdir(parents=True, exist_ok=True)
        reveal(folder)

    def on_restore(self) -> None:
        backups = list_backups(self.folder())
        if not backups:
            QMessageBox.information(
                self,
                "No backup yet",
                no_backup_text(self.study_id(), self.folder()),
            )
            return
        names = [b.name for b in backups]
        chosen, ok = QInputDialog.getItem(
            self, "Restore crosscheck", "Which saved copy (latest, or a day's copy)?", names, 0,
            False,
        )  # fmt: skip
        if ok:
            self.restore_requested.emit(backups[names.index(chosen)])
