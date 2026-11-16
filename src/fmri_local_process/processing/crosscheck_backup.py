"""Save and restore the crosscheck: the human decisions behind a BIDS folder, kept outside it.

Lessons from vr_lab_toolbox's "Save / Restore Crosscheck Data":

* Back up only what a human decided. The raw DICOMs and logs are already safe (never
  changed), and everything else in the BIDS folder is rebuilt from them by converting
  again. What can't be rebuilt is in code/: ID and log corrections, deleted sessions, notes,
  settings, expected lengths, kept runs and trims, and the action log.
  (ID, date and session corrections of DICOM folders included.)
* Keep the backup outside the BIDS folder, under the study's name rather than the folder's
  path, so it survives the BIDS folder being deleted, corrupted or moved.
* Restore = put the corrections back, convert from raw (which applies them: IDs, logs,
  deleted sessions), then replay what conversion can't redo (kept runs, trims). It is meant
  for an empty or freshly made BIDS folder, not a merge into one with its own changes.

Two additions: a save never drops a file from a backup (a BIDS folder that lost some of its
code/ files leaves the saved ones as they were, rather than wiping the last good copy; the
tool itself never deletes a record file, clearing one writes it empty), and besides
"latest", one dated copy per day is kept, so a bad day can be undone too.
"""

import shutil
import tempfile
from datetime import date
from pathlib import Path

from fmri_local_process.processing.action_log import ACTIONS_FILE, record_action
from fmri_local_process.processing.behav_match import CORRECTIONS_FILE
from fmri_local_process.processing.crosscheck import (
    DECISIONS_FILE,
    EXPECTED_FILE,
    NOTES_FILE,
    SETTINGS_FILE,
)
from fmri_local_process.processing.philani_bids import (
    DICOM_CORRECTIONS_FILE,
    DICOM_DATE_CORRECTIONS_FILE,
    DICOM_SESSION_CORRECTIONS_FILE,
    EXCLUDED_SESSIONS_FILE,
    SCAN_RULES_FILE,
)

# Every record of a human decision, relative to the BIDS folder.
RECORD_FILES = (
    DICOM_CORRECTIONS_FILE,
    DICOM_DATE_CORRECTIONS_FILE,
    DICOM_SESSION_CORRECTIONS_FILE,
    SCAN_RULES_FILE,
    CORRECTIONS_FILE,
    EXCLUDED_SESSIONS_FILE,
    NOTES_FILE,
    SETTINGS_FILE,
    EXPECTED_FILE,
    DECISIONS_FILE,
    ACTIONS_FILE,
)
LATEST = "latest"


def present_records(bids_root: Path) -> list[Path]:
    return [name for name in RECORD_FILES if (bids_root / name).is_file()]


def save_backup(bids_root: Path, backup_root: Path, today: date | None = None) -> list[Path]:
    """Copy the record files into backup_root/latest and backup_root/<today>.

    Returns the files saved; [] (and nothing touched) when the BIDS folder has none. Each
    copy is assembled beside its target and swapped in whole, so an interrupted save leaves
    the previous copy as it was.
    """
    names = present_records(bids_root)
    if not names:
        return []
    day = (today or date.today()).isoformat()
    for folder in (LATEST, day):
        target = backup_root / folder
        backup_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{folder}-", dir=backup_root))
        if target.is_dir():  # start from the saved copy: a record missing now isn't dropped
            shutil.copytree(target, staging, dirs_exist_ok=True)
        for name in names:
            (staging / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bids_root / name, staging / name)
        old = backup_root / f".{folder}-old"
        shutil.rmtree(old, ignore_errors=True)
        if target.exists():
            target.rename(old)
        staging.rename(target)
        shutil.rmtree(old, ignore_errors=True)
    return names


def list_backups(backup_root: Path) -> list[Path]:
    """The saved copies, newest first: "latest", then the dated ones."""
    if not backup_root.is_dir():
        return []
    dated = sorted(
        (p for p in backup_root.iterdir() if p.is_dir() and p.name[:1].isdigit()), reverse=True
    )
    latest = backup_root / LATEST
    return ([latest] if latest.is_dir() else []) + dated


def restore_backup(backup: Path, bids_root: Path) -> list[Path]:
    """Copy a saved copy's record files into the BIDS folder (replacing those there); the
    caller then converts again and replays the decisions."""
    restored = []
    for name in RECORD_FILES:
        source = backup / name
        if source.is_file():
            (bids_root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, bids_root / name)
            restored.append(name)
    if restored:
        record_action(bids_root, "restore crosscheck", str(backup), f"{len(restored)} file(s)")
    return restored
