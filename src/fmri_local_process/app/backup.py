"""The crosscheck backup: where it is kept, its settings and messages. The saving itself is
processing/crosscheck_backup.py."""

import os
import re
import sys
from pathlib import Path

DEFAULT_STUDY_ID = "philani"
DEFAULT_AUTOSAVE_MINUTES = 2
MAX_AUTOSAVE_MINUTES = 60
BACKUP_FOLDER = ("FmriLocalProcess", "crosscheck_backups")


def safe_name(text: str) -> str:
    """A study ID as a folder name: letters, digits, '-', '_' and '.' only."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text.strip()) or DEFAULT_STUDY_ID


def data_home() -> Path:
    """The user's local app-data folder: what Qt calls GenericDataLocation, which the window
    uses, so both front ends share one set of backups."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    share = os.environ.get("XDG_DATA_HOME")
    return Path(share) if share else Path.home() / ".local" / "share"


def backup_base() -> Path:
    """Where every study's backups live: outside any BIDS folder."""
    return data_home().joinpath(*BACKUP_FOLDER)


def saved_message(saved: int, folder: Path) -> str:
    return f"Crosscheck backup: {saved} file(s) to {folder}"


NOTHING_TO_SAVE = "Nothing to back up yet: no crosscheck decisions in this folder."


def no_backup_text(study: str, folder: Path) -> str:
    return f"No crosscheck backup saved for study '{study}' in {folder}."
