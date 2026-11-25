"""Crosscheck overview: the summary counts, each session's problems in a few words, and the
action log's filter."""

import re
from collections import Counter

from fmri_local_process.app.crosscheck import needs_a_look
from fmri_local_process.processing.crosscheck import Issue, SessionReview, overview_tasks

# Cell background per status; black text on these reads in light and dark themes alike.
CELL_COLORS = {
    "missing": "#f1948a",
    "short": "#f9e79f",
    "pending": "#f5cba7",
    "check": "#f5cba7",
    "long": "#aed6f1",
    "extra": "#a3e4d7",
    "trimmed": "#abebc6",
}
CELL_LEGEND = (
    "Red: missing (no scan, no events, no T1w)\n"
    "Yellow: shorter than expected, or no events\n"
    "Orange: several runs to pick from, or a T1w not defaced\n"
    "Blue: longer than expected (trim it)\n"
    "Green ✂: trimmed, now the expected length (done)"
)
SUMMARY_MAX_LINES = 14
# A session's problems in a few words: '<what>: <which scans>', most urgent first.
PROBLEM_WORDS = {
    "missing": "missing",
    "short": "short",
    "pending": "keep one of",
    "long": "trim",
    "check": "check log of",
    "extra": "extra session",
}
PROBLEMS_LEGEND = (
    "missing: an expected file isn't there\n"
    "short: shorter than expected (or than the same scan elsewhere), or no events\n"
    "keep one of: a scan converted as several runs\n"
    "trim: longer than expected\n"
    "check log of: events from a log dated other than the scan\n"
    "extra session: the subject has more sessions than expected\n"
    "crosschecked: marked as reviewed (whatever its problems)"
)
OWNER_PREFIX = re.compile(r"^sub-[^_]+_(?:ses-[^_]+_)?")
SESSION_KINDS = {
    "pending": "session(s) with repeated runs to pick",
    "short": "session(s) with a short scan or empty events",
    "long": "session(s) with runs to trim",
    "missing": "session(s) with missing files",
    "check": "session(s) with a log dated other than the scan",
}


def issue_target(issue: Issue) -> str:
    """The scan an issue is about, shortly: 'task-Rest_bold', 'T1w'; '' when about none."""
    if issue.kind == "extra":
        return ""
    if issue.path is not None:
        name = issue.path.name.split(".", 1)[0]
    else:
        name = re.split(r"[:(]", issue.text, maxsplit=1)[0].strip()
        name = name.removesuffix(".tsv").removesuffix(".json")
    return OWNER_PREFIX.sub("", name)


def problem_summary(review: SessionReview) -> str:
    """'trim: task-Rest_bold; missing: task-Matching_events', 'ok' or 'crosschecked'."""
    if review.crosschecked:
        return "crosschecked"
    parts = []
    for kind, word in PROBLEM_WORDS.items():
        targets = list(dict.fromkeys(issue_target(i) for i in review.issues if i.kind == kind))
        named = [t for t in targets if t]
        if named:
            parts.append(f"{word}: {', '.join(named)}")
        elif targets:
            parts.append(word)
    return "; ".join(parts) or "ok"


def summary_lines(reviews: list[SessionReview], expected: int, excluded: int) -> list[str]:
    """Counts for the whole folder, as the old BIDS viewer's summary gave them."""
    sessions = [r for r in reviews if r.session is not None]
    subjects = {r.subject for r in sessions}
    per_subject = Counter(r.subject for r in sessions)
    clean = sum(not r.issues for r in sessions)
    marked = sum(r.crosschecked for r in sessions)
    waiting = sum(needs_a_look(r) for r in sessions)
    lines = [
        f"{len(subjects)} subject(s), {len(sessions)} session(s); "
        f"{expected} session(s) per subject expected",
        f"{clean} session(s) with nothing to resolve; {marked} marked crosschecked; "
        f"{waiting} still to check",
    ]
    with_t1w = {r.subject for r in sessions if any(d.label == "T1w" for d in r.files)}
    lines.append(f"Subjects with at least 1 T1w: {len(with_t1w)}")
    for task in overview_tasks(sessions):
        label = f"task-{task}_bold"
        with_task = {r.subject for r in sessions if any(d.label == label for d in r.files)}
        lines.append(f"Subjects with at least 1 {task} run: {len(with_task)}")
    over = sum(n > expected for n in per_subject.values())
    under = sum(n < expected for n in per_subject.values())
    if over:
        lines.append(f"{over} subject(s) with more sessions than expected")
    if under:
        lines.append(f"{under} subject(s) with fewer sessions than expected (not converted yet?)")
    for kind, name in SESSION_KINDS.items():
        if affected := sum(any(i.kind == kind for i in r.issues) for r in sessions):
            lines.append(f"{affected} {name}")
    if excluded:
        lines.append(f"{excluded} DICOM session folder(s) deleted at BIDS Crosscheck (left out)")
    return lines


def matches_search(action: dict[str, str], search: str) -> bool:
    """Every word of the search box is somewhere in the logged action."""
    text = " ".join(str(value) for value in action.values()).lower()
    return all(word in text for word in search.lower().split())
