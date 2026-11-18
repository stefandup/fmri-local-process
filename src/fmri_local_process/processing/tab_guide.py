"""The toolbox's tabs: their names, order and the one line each opens with, and which a
review package shows. Free of Qt, so the review package's README can list them too.
"""

from collections.abc import Collection

SETUP = "Setup & Convert to BIDS"
SESSIONS = "Raw DICOMs"
BEHAV = "Behaviour logs"
SCAN_LENGTHS = "Scan lengths & trimming"
CROSSCHECK = "BIDS Crosscheck"
MRIQC = "Scan quality (MRIQC)"
PREPROCESS = "Preprocess BIDS (C-PAC)"
OVERVIEW = "Crosscheck overview"
FIRST_LEVEL = "First-level stats"
SEQUENCE = "Sequence overview"
TAB_ORDER = (
    SETUP, SESSIONS, SEQUENCE, BEHAV, SCAN_LENGTHS, CROSSCHECK, MRIQC, PREPROCESS, OVERVIEW,
    FIRST_LEVEL,
)  # fmt: skip
# The first thing each tab says: what it is for (newcomers to the data read this first).
TAB_INTROS = {
    SETUP: "Choose the data folders, then convert the raw DICOM scans into a BIDS dataset.",
    SESSIONS: "The raw scanner sessions per participant (DICOM folders): IDs, dates and scans.",
    SEQUENCE: "A participant's runs of one scan side by side, with their scan settings: do "
    "they match?",
    BEHAV: "The task logs from the experiment, and which scan session each belongs to.",
    SCAN_LENGTHS: "How long each scan is against the expected length; trim scans that ran on "
    "too long. The task starts with the scanner trigger and the dummy scans are discarded "
    "before it, so there are no dummy scans to trim.",
    CROSSCHECK: "Go through each session's problems and mark it crosschecked once it is right.",
    MRIQC: "Image quality of each raw scan: select a run, look at it, and rate it 1 (bad: "
    "discard), 2 (unsure) or 3 (keep).",
    PREPROCESS: "Preprocess the BIDS dataset with C-PAC (in Docker), one subject at a time.",
    OVERVIEW: "Every session in one table: which scans are there, how long, and what still "
    "needs a look.",
    FIRST_LEVEL: "Does the task give the expected brain response? The first subject runs at "
    "startup; pick another and Run to compare its runs.",
}
HIDDEN_WHEN_VIEW_ONLY = (SETUP, PREPROCESS)  # they only change data
# A review package without this folder leaves the tab out: there is nothing to show on it.
NEEDS_PACKAGED_FOLDER = {SESSIONS: "dicom", BEHAV: "behav"}


def shown_tabs(view_only: bool, packaged: Collection[str] | None) -> list[str]:
    """The tabs to show, in order. packaged: the folder keys a review package holds (None
    for a normal install, which shows every tab but those view-only hides)."""
    left_out = set(HIDDEN_WHEN_VIEW_ONLY) if view_only else set()
    if packaged is not None:
        left_out |= {name for name, key in NEEDS_PACKAGED_FOLDER.items() if key not in packaged}
    return [name for name in TAB_ORDER if name not in left_out]
