"""What a tab says it is missing, with where to fix it."""

from pathlib import Path


def bids_notices(bids_root: Path | None, has_sessions: bool) -> list[str]:
    """The banner of a tab that reads the BIDS folder's sessions."""
    if bids_root is None:
        return ["No BIDS folder chosen: choose it on the Setup & Convert to BIDS tab."]
    if not has_sessions:
        return [
            f"No converted sessions in {bids_root} yet: convert the DICOMs on the Setup & "
            "Convert to BIDS tab first."
        ]
    return []
