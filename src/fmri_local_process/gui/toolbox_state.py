"""What the toolbox's tabs share: the folders, what is running, and when the BIDS folder
last changed.

* Folders (DICOM sessions, behavioural logs, BIDS, C-PAC output) are chosen once, on the
  Setup tab, and remembered. The first time, they are taken over from the separate
  crosscheck and preprocess windows' remembered folders.
* A job that changes the BIDS folder (converting, trimming, C-PAC reading it) is registered
  as busy; the other tabs lock their own changing actions until it ends.
* Every change to the BIDS folder bumps a generation number, so a tab refreshes when it is
  shown next rather than on every change.
"""

from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

from fmri_local_process.gui.qt_common import SETTINGS_ORGANIZATION

SETTINGS_APP = "FmriToolbox"
FOLDER_KEYS = ("dicom", "behav", "bids", "output")
# Where the separate windows kept their folders before the toolbox: {app: {their key: ours}}.
OLD_SETTINGS = {
    "FmriCrosscheck": {"bids": "bids", "dicom": "dicom", "behav": "behav"},
    "FmriPreprocess": {"bids": "bids", "output": "output"},
}

# Jobs that can be busy; the text is what the tabs say while it runs.
CONVERTING = "converting"
TRIMMING = "trimming"
PREPROCESSING = "preprocessing"
JOB_TEXT = {
    CONVERTING: "Converting DICOMs to BIDS",
    TRIMMING: "Trimming runs",
    PREPROCESSING: "C-PAC is preprocessing",
}


def make_settings(app: str) -> QSettings:
    """The settings store for app (a function, so tests can point it at a temp file)."""
    return QSettings(SETTINGS_ORGANIZATION, app)


class ToolboxState(QObject):
    folder_changed = Signal(str)  # key
    busy_changed = Signal()
    data_changed = Signal()  # something in the BIDS folder changed

    def __init__(self) -> None:
        super().__init__()
        self.settings = make_settings(SETTINGS_APP)
        self.folders: dict[str, Path | None] = {}
        for key in FOLDER_KEYS:
            saved = str(self.settings.value(key, "") or "") or self._old_folder(key)
            self.folders[key] = Path(saved) if saved and Path(saved).is_dir() else None
        self.busy: dict[str, str] = {}  # job -> text
        self.generation = 0

    @staticmethod
    def _old_folder(key: str) -> str:
        for app, keys in OLD_SETTINGS.items():
            for old_key, new_key in keys.items():
                if new_key == key and (value := str(make_settings(app).value(old_key, "") or "")):
                    return value
        return ""

    # ---- folders ------------------------------------------------------------------------------

    def folder(self, key: str) -> Path | None:
        return self.folders.get(key)

    def set_folder(self, key: str, folder: Path | None) -> None:
        if self.folders.get(key) == folder:
            return
        self.folders[key] = folder
        self.settings.setValue(key, str(folder) if folder else "")
        self.folder_changed.emit(key)
        if key == "bids":
            self.bump()

    # ---- busy jobs ----------------------------------------------------------------------------

    def set_busy(self, job: str, running: bool) -> None:
        if running == (job in self.busy):
            return
        if running:
            self.busy[job] = JOB_TEXT.get(job, job)
        else:
            del self.busy[job]
        self.busy_changed.emit()

    def lock_reason(self, *allowed: str) -> str:
        """Why changing actions are locked now ('' if they aren't); jobs in `allowed`
        (usually the asking tab's own) don't count."""
        texts = [text for job, text in self.busy.items() if job not in allowed]
        return "; ".join(texts)

    # ---- BIDS changes -------------------------------------------------------------------------

    def bump(self) -> None:
        self.generation += 1
        self.data_changed.emit()
