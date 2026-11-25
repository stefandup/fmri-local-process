"""The toolbox's tabs for the window and the terminal app: one list, so both stay in step. (The
names themselves are in processing/tab_guide.py, which the review package's README uses too.)"""

from pathlib import Path

from fmri_local_process.app.state import VIEW_ONLY, ToolboxState
from fmri_local_process.processing.review_mode import package_folders, package_name, package_root
from fmri_local_process.processing.tab_guide import (  # noqa: F401 -- re-exported
    BEHAV,
    CROSSCHECK,
    FIRST_LEVEL,
    HIDDEN_WHEN_VIEW_ONLY,
    MRIQC,
    NEEDS_PACKAGED_FOLDER,
    OVERVIEW,
    PREPROCESS,
    SCAN_LENGTHS,
    SEQUENCE,
    SESSIONS,
    SETUP,
    TAB_INTROS,
    TAB_ORDER,
    shown_tabs,
)

TITLE = "Philani fMRI toolbox"
# Tabs the terminal app doesn't have yet: the parity test allows only these to be missing.
NOT_YET_IN_TUI: tuple[str, ...] = ()


def tab_text(number: int, name: str, badge: str = "") -> str:
    """'4. Crosscheck (6 to check)'"""
    return f"{number}. {name}" + (f" ({badge})" if badge else "")


def tab_names(view_only: bool) -> list[str]:
    """The tabs to show here, in order (a review package leaves some out)."""
    return shown_tabs(view_only, package_folders() if package_root() is not None else None)


def title_text(view_only: bool) -> str:
    review = package_name()
    return (
        TITLE
        + (f"  —  review package: {review}" if review else "")
        + (" (view only)" if view_only else "")
    )


def header_texts(state: ToolboxState) -> tuple[str, str, str]:
    """(BIDS folder name, where it is, what is running) for the line above the tabs."""
    bids_root: Path | None = state.folder("bids")
    busy = state.lock_reason(VIEW_ONLY)  # the title says view only
    return (
        bids_root.name if bids_root else "No BIDS folder chosen",
        str(bids_root.parent) if bids_root else "(first tab)",
        f"⏳ {busy}" if busy else "",
    )
