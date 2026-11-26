"""BIDS Crosscheck: the texts of the session list, the problems and the confirmations."""

from collections.abc import Callable
from pathlib import Path

from fmri_local_process.processing.crosscheck import (
    CrosscheckError,
    SessionReview,
    delete_session,
)

ICONS = {
    "pending": "⚠",
    "short": "❗",
    "long": "✂",
    "missing": "○",
    "check": "⚑",
    "extra": "⊕",
}
COLORS = {
    "pending": "#f39c12",
    "short": "#e74c3c",
    "long": "#3498db",
    "missing": "#9b59b6",
    "check": "#e67e22",
    "extra": "#16a085",
    "ok": "#2ecc71",
}
TABLE_COLUMNS = ("ID", "Date", "Session", "Files", "Problems", "Note")
PROBLEMS_COLUMN = 4
NOTE_COLUMN = 5
MAX_LISTED = 15
OK_ICON = "●"
CROSSCHECKED_ICON = "☑"  # vr_lab_toolbox's mark for "reviewed by a human"
CROSSCHECKED_COLOR = "#3498db"
LEGEND = (
    f"{CROSSCHECKED_ICON} crosschecked: marked as reviewed by you\n"
    f"{OK_ICON} nothing to resolve\n"
    f"{ICONS['pending']} a scan converted as several runs: keep one\n"
    f"{ICONS['short']} shorter than expected (or than the same scan elsewhere), or no events\n"
    f"{ICONS['long']} longer than expected: trim it\n"
    f"{ICONS['missing']} expected file missing\n"
    f"{ICONS['check']} events from a log dated other than the scan: check it's the right log\n"
    f"{ICONS['extra']} the subject has more sessions than expected: is one a dud to delete?"
)
HELP_TEXT = (
    "Click a session to see its files and problems on the right. Once you have been "
    "through it, <b>Mark crosschecked</b> (it then shows "
    f"{CROSSCHECKED_ICON}, whatever its problems). Tick sessions (the box beside the ID; "
    "<b>Select all</b> / <b>Select none</b>) to mark, note or delete several at once. "
    "Deleting a session leaves its DICOM folder out of conversion from now on; converting "
    "again renumbers the remaining sessions. Every change is logged (Overview tab, Action "
    "log) and kept in the crosscheck backup (tab 1).<br><br>"
    "<b>Problems</b>:<br>" + LEGEND.replace("\n", "<br>")
)


def problem_text(review: SessionReview) -> str:
    if review.crosschecked:
        return f"{CROSSCHECKED_ICON} crosschecked"
    counts = {kind: sum(i.kind == kind for i in review.issues) for kind in ICONS}
    parts = [f"{ICONS[kind]} {n}" for kind, n in counts.items() if n]
    return "   ".join(parts) or f"{OK_ICON} ok"


def needs_a_look(review: SessionReview) -> bool:
    """Problems, and not marked as crosschecked yet."""
    return bool(review.issues) and not review.crosschecked


def problem_color(review: SessionReview) -> str:
    if review.crosschecked:
        return CROSSCHECKED_COLOR
    kinds = {i.kind for i in review.issues}
    order = ("short", "pending", "check", "extra", "long", "missing")
    return next((COLORS[k] for k in order if k in kinds), COLORS["ok"])


def review_cells(review: SessionReview) -> tuple[str, ...]:
    """One row of the session list (TABLE_COLUMNS)."""
    return (
        review.subject,
        review.date or "?",
        f"ses-{review.session}" if review.session else "-",
        str(len(review.files) + len(review.pending)),
        problem_text(review),
        review.note,
    )


def badge_text(reviews: list[SessionReview]) -> str:
    waiting = sum(needs_a_look(r) for r in reviews)
    return f"{waiting} to check" if waiting else ""


def summary_text(reviews: list[SessionReview], bids_root: Path | None) -> str:
    if bids_root is None:
        return "Choose the BIDS folder on the Setup & Convert to BIDS tab."
    waiting = sum(needs_a_look(r) for r in reviews)
    marked = sum(r.crosschecked for r in reviews)
    return f"{len(reviews)} session(s): {marked} crosschecked, {waiting} still to check"


def scanned_text(review: SessionReview) -> str:
    scanned = f"scanned {review.date}" if review.date else "scan date unknown"
    source = f" from {review.source_folder}" if review.source_folder else ""
    return f"{scanned}{source}"


def mark_text(reviews: list[SessionReview], marked: bool) -> str:
    names = ", ".join(r.name for r in reviews[:MAX_LISTED])
    return f"{'Marked crosschecked' if marked else 'Un-marked'}: {names}"


def note_text(reviews: list[SessionReview], text: str) -> str:
    names = ", ".join(r.name for r in reviews)
    return f"Note for {names}: {text.strip() or '(cleared)'}"


def delete_question(reviews: list[SessionReview]) -> str:
    listing = "\n".join(
        f"  {r.name}  scanned {r.date or '?'}  from {r.source_folder or '?'}"
        + (f"  ({len(r.issues)} problem(s))" if r.issues else "")
        for r in reviews[:MAX_LISTED]
    )
    more = f"\n  ... and {len(reviews) - MAX_LISTED} more" if len(reviews) > MAX_LISTED else ""
    return (
        f"Delete {len(reviews)} session folder(s) from the BIDS folder?\n{listing}{more}\n\n"
        "Their DICOM folders are left out of conversion from now on (listed in "
        "code/excluded_sessions.json; remove a line there to convert it again). Convert "
        "to BIDS afterwards to renumber the remaining sessions."
    )


def delete_sessions(
    bids_root: Path, reviews: list[SessionReview], tell: Callable[[str], object]
) -> tuple[list[str], list[str]]:
    """Delete each session folder, logging as it goes: (names deleted, why others weren't)."""
    deleted, errors = [], []
    for review in reviews:
        try:
            delete_session(bids_root, review, review.note)
        except (CrosscheckError, OSError) as e:
            errors.append(str(e))
            tell(f"ERROR  deleting {review.name}: {e}")
            continue
        deleted.append(review.name)
        tell(f"Deleted {review.name} (from {review.source_folder})")
    tell("Convert to BIDS to renumber the remaining sessions.")
    return deleted, errors
