"""Scan rules: the help, and what the rules find in a DICOM folder (folder names only)."""

from pathlib import Path

from fmri_local_process.processing.philani_bids import (
    IGNORED_FOLDERS,
    ScanRule,
    match_series,
    series_folder_names,
)

HELP_TEXT = (
    "Each row says which DICOM series folders become which BIDS scan.<br><br>"
    "<b>Folder pattern</b>: matched against each folder's path, case-sensitively. "
    "<b>*</b> stands for anything, <b>?</b> for one character. The Philani patterns start "
    "with * and end with the end of the series name, e.g. <b>*_rest_AP</b> matches "
    "<i>.../5_rest_AP</i>. <b>*t1_mprage*</b> matches any folder with t1_mprage in its "
    "name.<br><br>"
    "<b>BIDS scan</b>: T1w, T2w or FLAIR (anat), or task-&lt;Name&gt;_bold (func), e.g. "
    "task-Rest_bold.<br><br>"
    "Several rows may make the same scan; a session where more than one series matches "
    "gets them as runs to pick from at the BIDS Crosscheck. Changes apply from the next "
    "<b>Convert to BIDS</b>: scans converted already aren't renamed. Every change is logged "
    "and kept in the crosscheck backup."
)


def check_text(dicom_root: Path, rules: tuple[ScanRule, ...]) -> str:
    """Per session folder: the scans the rules find, and the series folders they miss."""
    lines = []
    for folder in sorted(dicom_root.iterdir()):
        if folder.name in IGNORED_FOLDERS or not folder.is_dir():
            continue
        matches = match_series(folder, rules)
        found = [f"{rule.scan} <- {', '.join(s.name for s in series)}" for rule, series in
                 matches.items()]  # fmt: skip
        used = {s.name for series in matches.values() for s in series}
        missed = [name for name in series_folder_names(folder) if name not in used]
        lines.append(folder.name)
        lines += [f"    {line}" for line in found] or ["    (nothing matches)"]
        if missed:
            lines.append(f"    not matched: {', '.join(missed)}")
    return "\n".join(lines) or "No session folders in the DICOM folder."
