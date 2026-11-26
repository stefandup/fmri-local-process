"""What the toolbox's tabs share: the folders, what is running, and when the BIDS folder
last changed.

* Folders (DICOM sessions, behavioural logs, BIDS, C-PAC output) are chosen once, on the
  Setup tab, and remembered (the MRIQC output folder on the MRIQC tab). The first time,
  they are taken over from the separate crosscheck and preprocess windows' remembered
  folders. In a review package they start on its own data (processing/review_mode.py).
* A job that changes the BIDS folder (converting, trimming, C-PAC reading it) is registered
  as busy; the other tabs lock their own changing actions until it ends.
* Every change to the BIDS folder bumps a generation number, so a tab refreshes when it is
  shown next rather than on every change.
"""

from pathlib import Path

from fmri_local_process.app.events import Event
from fmri_local_process.app.settings import MakeSettings
from fmri_local_process.processing.review_mode import package_folders

SETTINGS_APP = "FmriToolbox"
FOLDER_KEYS = ("dicom", "behav", "bids", "output", "mriqc")
# Where the separate windows kept their folders before the toolbox: {app: {their key: ours}}.
OLD_SETTINGS = {
    "FmriCrosscheck": {"bids": "bids", "dicom": "dicom", "behav": "behav"},
    "FmriPreprocess": {"bids": "bids", "output": "output"},
}

# Jobs that can be busy; the text is what the tabs say while it runs.
CONVERTING = "converting"
TRIMMING = "trimming"
PREPROCESSING = "preprocessing"
QUALITY_CHECKING = "quality checking"  # MRIQC reading the BIDS folder
VIEW_ONLY = "view only"  # never ends: a view-only review package (review_mode.view_only)
JOB_TEXT = {
    CONVERTING: "Converting DICOMs to BIDS",
    TRIMMING: "Trimming runs",
    PREPROCESSING: "C-PAC is preprocessing",
    QUALITY_CHECKING: "MRIQC is checking the scans",
    VIEW_ONLY: "View-only review package",
}

# Pairs of folders that must not be the same or inside each other: (key, key, why)
CONFLICTS = (
    ("bids", "dicom", "the converter would read its own output as DICOM sessions"),
    ("bids", "output", "C-PAC output can't go in (or around) the BIDS folder"),
    ("dicom", "output", "C-PAC output can't go in (or around) the raw DICOMs"),
    ("mriqc", "bids", "MRIQC output can't go in (or around) the BIDS folder"),
    ("mriqc", "dicom", "MRIQC output can't go in (or around) the raw DICOMs"),
    ("mriqc", "output", "MRIQC output needs its own folder, apart from C-PAC's"),
)
# Folders read only: they may change while a job runs; the others wait for it.
READ_ONLY_KEYS = ("behav",)


class ToolboxState:
    def __init__(self, make_settings: MakeSettings) -> None:
        self.folder_changed = Event()  # key
        self.busy_changed = Event()
        self.data_changed = Event()  # something in the BIDS folder changed
        self.make_settings = make_settings
        self.settings = make_settings(SETTINGS_APP)
        self.folders: dict[str, Path | None] = {}
        packaged = package_folders()  # a review package: start on its data
        for key in FOLDER_KEYS:
            saved = str(self.settings.value(key, "") or "") or self._old_folder(key)
            if saved and Path(saved).is_dir():
                self.folders[key] = Path(saved)
            else:  # e.g. a package unzipped elsewhere: its own data again
                self.folders[key] = packaged.get(key)
        self.busy: dict[str, str] = {}  # job -> text
        self.generation = 0

    def _old_folder(self, key: str) -> str:
        for app, keys in OLD_SETTINGS.items():
            for old_key, new_key in keys.items():
                if new_key != key:
                    continue
                if value := str(self.make_settings(app).value(old_key, "") or ""):
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
        self.settings.sync()  # on disk now (a review package's settings file, too)
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


def paths_conflict(first: Path, second: Path) -> bool:
    """True when the folders are the same or one contains the other."""
    a, b = first.resolve(), second.resolve()
    return a == b or a in b.parents or b in a.parents


def folder_conflict(state: ToolboxState, key: str, folder: Path) -> str:
    """Why folder can't be used for key ('' if it can)."""
    for first, second, why in CONFLICTS:
        if key in (first, second):
            other = state.folder(second if key == first else first)
            if other is not None and paths_conflict(folder, other):
                return why
    return ""


def folder_locked(state: ToolboxState, key: str) -> bool:
    """A running job keeps its folders; read-only ones may change meanwhile."""
    return bool(state.lock_reason()) and key not in READ_ONLY_KEYS
