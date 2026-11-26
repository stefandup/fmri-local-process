"""Scan lengths & trimming: how many volumes each BOLD scan should have, worked out from its
runs, the study's design and the behaviour logs, or set by the human."""

from collections import Counter
from pathlib import Path

from fmri_local_process.processing.crosscheck import (
    DEFAULT_EVENTS_MARGIN_S,
    EXPECTED_FILE,
    SETTINGS_FILE,
    DataFile,
    RunEnd,
    automatic_lengths,
    read_events_margins,
    read_expected,
    run_event_ends,
    volumes_from_logs,
    write_events_margins,
    write_expected,
)

MAX_VOLUMES = 100_000
MAX_MARGIN_S = 120.0
LONG_COLOR = "#3498db"  # the crosscheck's colour for "long"
TRIMMED_COLOR = "#27ae60"  # done: green


def scan_lengths(data_files: list[DataFile]) -> dict[str, list[DataFile]]:
    """The 4D scans in the BIDS folder (not staged ones), by label."""
    scans: dict[str, list[DataFile]] = {}
    for data in data_files:
        if not data.pending and data.volumes is not None and data.volumes > 1:
            scans.setdefault(data.label, []).append(data)
    return dict(sorted(scans.items()))


def length_note(runs: list[DataFile]) -> str:
    """'441 (x3), 520, 33' -- the commonest lengths first."""
    counts = Counter(d.volumes for d in runs).most_common()
    return ", ".join(f"{n} (x{c})" if c > 1 else str(n) for n, c in counts)


def trimmed_note(runs: list[DataFile]) -> str:
    """'Trimmed 3 of 5' (all: 'Trimmed (all 5)'), or '' when none of the runs was trimmed."""
    trimmed = sum(1 for d in runs if d.trimmed_from)
    if not trimmed:
        return ""
    return (
        f"Trimmed (all {trimmed})" if trimmed == len(runs) else f"Trimmed {trimmed} of {len(runs)}"
    )


HELP_TEXT = (
    "How many volumes each BOLD scan should have. By default the median of its runs (with a "
    "single run, the study's usual length). The checkerboard instead uses its task design: "
    "200 s at the runs' TR, rounded up (task design). Type another number to set "
    "it, or step down to the bottom to go back to the automatic length; it is saved as you "
    "type.<br><br>"
    "Runs with fewer volumes are flagged as short at the BIDS Crosscheck. Runs with more can "
    "be trimmed to this length with <b>Trim</b>, keeping the first volumes (the task starts "
    "with the scanner trigger, and the dummy scans are discarded before it, so the first "
    "volume is the task's start: no dummy scans to trim): the cut volumes "
    "are deleted from the BIDS folder (the raw DICOMs keep them; delete the file and convert "
    "again to undo). Trim here, before the crosscheck, so BIDS is right before it is "
    "preprocessed.<br><br>"
    "<b>From logs</b> suggests a length from the complete behaviour logs (read on the "
    "Behaviour logs tab; logs marked error / not used left out; until they are read, the "
    "events.tsv files in BIDS): each log's last event end plus the <b>Margin</b> (default "
    f"{DEFAULT_EVENTS_MARGIN_S:g} s, so the BOLD response to the last trials is kept), in "
    "volumes at its TR; the median over runs, so one log that stopped early doesn't set it. "
    "It's only a suggestion: type it into Expected volumes if it fits. Scans without logs "
    "(e.g. rest) show none."
)


def trimmed_details(runs: list[DataFile]) -> str:
    return "\n".join(
        f"{d.path.name}: {d.trimmed_from} -> {d.volumes} volumes" for d in runs if d.trimmed_from
    )


COLUMNS = (
    "Scan",
    "Runs",
    "Volumes found",
    "Expected volumes",
    "From logs",
    "Margin (s)",
    "Minutes",
    "Too long",
    "Trimmed",
)


class ScanLengths:
    """The numbers behind the tab for one BIDS folder."""

    def __init__(
        self,
        bids_root: Path,
        data_files: list[DataFile],
        log_ends: dict[str, list[RunEnd]] | None = None,
    ) -> None:
        self.bids_root = bids_root
        self.scans = scan_lengths(data_files)
        self.automatic = automatic_lengths(data_files)  # median of runs, else study default
        # Read once (margins only rescale them): the complete raw logs the Behaviour logs tab
        # read, else the events.tsv files already in BIDS.
        self.log_ends = log_ends or {}
        self.event_ends = self.log_ends | {
            label: ends
            for label, ends in run_event_ends(data_files).items()
            if label not in self.log_ends
        }
        saved, margins = read_expected(bids_root), read_events_margins(bids_root)
        # Only numbers the human set: a scan left at "median" keeps following its runs.
        self.set = {label: saved.get(label, 0) for label in self.scans}
        self.margins = {
            label: margins.get(label, DEFAULT_EVENTS_MARGIN_S)
            for label in self.scans
            if label in self.event_ends
        }

    def automatic_text(self, label: str) -> str:
        if label not in self.automatic:
            return "not set"
        volumes, source = self.automatic[label]
        return f"{'median' if source == 'median of runs' else source} ({volumes})"

    def expected(self, label: str) -> int | None:
        """The number typed for a scan, else its median or study default (None if neither)."""
        automatic = self.automatic.get(label)
        return self.set.get(label) or (automatic[0] if automatic else None)

    def too_long(self, label: str) -> list[DataFile]:
        expected = self.expected(label)
        if not expected:
            return []
        return [d for d in self.scans[label] if d.volumes is not None and d.volumes > expected]

    def too_long_details(self, label: str) -> str:
        return "\n".join(f"{d.path.name}: {d.volumes} volumes" for d in self.too_long(label))

    def long_count(self) -> int:
        return sum(len(self.too_long(label)) for label in self.scans)

    def minutes(self, label: str) -> str:
        volumes = self.expected(label)
        tr = next((d.repetition_time for d in self.scans[label] if d.repetition_time), None)
        return f"{volumes * tr / 60:.1f}" if tr and volumes else ""

    def from_logs(self, label: str) -> tuple[str, str]:
        """(the volumes the logs suggest at the scan's margin, what that rests on)."""
        ends, margin = self.event_ends.get(label, []), self.margins.get(label, 0.0)
        suggested = volumes_from_logs(ends, margin)
        if suggested is None:
            return "-", (
                "No complete behaviour log for this scan (Behaviour logs tab), and no "
                "events.tsv next to its runs."
            )
        volumes, longest = suggested
        source = (
            "complete behaviour logs"
            if label in self.log_ends
            else "events.tsv files in BIDS (the Behaviour logs tab hasn't read the logs)"
        )
        return str(volumes), (
            f"Median over {len(ends)} {source}: last event + {margin:g} s, in volumes at "
            f"the scan's TR. Longest: {longest}."
        )

    def save(self) -> None:
        # Keep values for scans not in the folder right now (e.g. all runs deleted).
        kept = {k: v for k, v in read_expected(self.bids_root).items() if k not in self.scans}
        write_expected(self.bids_root, kept | {k: v for k, v in self.set.items() if v})
        kept_margins = {
            k: v for k, v in read_events_margins(self.bids_root).items() if k not in self.margins
        }
        write_events_margins(self.bids_root, kept_margins | self.margins)

    def saved_text(self) -> str:
        return (
            f"Saved in {self.bids_root / EXPECTED_FILE} (margins in "
            f"{self.bids_root / SETTINGS_FILE})."
        )


def no_scans_notices(bids_root: Path | None, has_scans: bool) -> list[str]:
    if bids_root is None:
        return ["No BIDS folder chosen: choose it on the Setup & Convert to BIDS tab."]
    if not has_scans:
        return [
            f"No BOLD scans in {bids_root} yet: convert the DICOMs on the Setup & Convert "
            "to BIDS tab first."
        ]
    return []
