"""A record of what people changed in a BIDS folder at crosscheck.

Every action that deletes, trims, moves or corrects something (keeping a run, deleting a
file or a session, trimming, ID / log corrections, notes, settings) adds one row to
code/crosscheck_actions.tsv in the BIDS folder: when, who (the computer login), what, on
which file or folder, and the details. The file is only ever appended to, so it travels
with the data as its history.
"""

import csv
import getpass
from datetime import datetime
from pathlib import Path

ACTIONS_FILE = Path("code") / "crosscheck_actions.tsv"
COLUMNS = ("time", "user", "action", "target", "detail")


def current_user() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # no login name in the environment
        return ""


def record_action(bids_root: Path, action: str, target: str, detail: str = "") -> None:
    """Append one row to the BIDS folder's action log (created with a header if new)."""
    path = bids_root / ACTIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        if is_new:
            writer.writerow(COLUMNS)
        when = datetime.now().isoformat(timespec="seconds")
        writer.writerow([when, current_user(), action, target, detail])


def read_actions(bids_root: Path) -> list[dict[str, str]]:
    """The logged actions, oldest first ([] if none yet)."""
    try:
        with (bids_root / ACTIONS_FILE).open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter="\t"))
    except (OSError, csv.Error, UnicodeDecodeError):
        return []
