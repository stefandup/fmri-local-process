"""Trimming long BOLD runs, shared by the Crosscheck and Scan lengths tabs: one at a time (each
run is a few seconds of gzip), after the human confirms."""

from collections.abc import Callable
from pathlib import Path

from fmri_local_process.processing.crosscheck import CrosscheckError, DataFile, trim_run

MAX_LISTED = 15


def confirm_text(jobs: list[tuple[DataFile, int]]) -> str:
    names = "\n".join(f"  {d.path.name}: {d.volumes} -> {n}" for d, n in jobs[:MAX_LISTED])
    more = f"\n  ... and {len(jobs) - MAX_LISTED} more" if len(jobs) > MAX_LISTED else ""
    return (
        f"Trim {len(jobs)} run(s), keeping the first volumes?\n{names}{more}\n\n"
        "The cut volumes are deleted from the BIDS folder (the raw DICOMs keep them: "
        "delete the file and convert again to restore it)."
    )


class TrimJob:
    """Trims runs one by one; reports progress as log lines."""

    def __init__(
        self, bids_root: Path, jobs: list[tuple[DataFile, int]], tell: Callable[[str], object]
    ) -> None:
        self.bids_root, self.jobs, self.tell = bids_root, jobs, tell

    def run(self) -> None:
        for number, (data, volumes) in enumerate(self.jobs, start=1):
            try:
                trim_run(self.bids_root, data, volumes)
            except (CrosscheckError, OSError, ValueError) as e:
                self.tell(f"NOT TRIMMED  {e}")
                continue
            self.tell(f"Trimmed {data.path.name} to {volumes} volumes ({number}/{len(self.jobs)})")
