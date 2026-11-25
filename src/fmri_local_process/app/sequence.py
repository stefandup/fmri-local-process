"""Sequence overview: reading a subject's scan parameters, and the log shown."""

from pathlib import Path

from fmri_local_process.processing import sequence_params as sp


class ReadJob:
    """Headers and sidecars of a subject's runs; with a DICOM folder, their DICOMs too."""

    def __init__(self, runs: list[sp.ScanRun], bids_root: Path, dicom_root: Path | None) -> None:
        self.runs, self.bids_root, self.dicom_root = runs, bids_root, dicom_root

    def run(self) -> str:
        """Fills in the runs; why not all could be read, "" when they were."""
        try:
            sp.read_values(self.runs)
            if self.dicom_root is not None:
                sp.add_dicom_values(self.runs, self.dicom_root, self.bids_root)
        except (OSError, ImportError) as e:
            return str(e)
        return ""


def overview_text(runs: list[sp.ScanRun], subject: str, scan: str, only_differences: bool) -> str:
    if not runs:
        return "No runs found for this subject." if subject else ""
    rows = sp.table_rows(runs)
    title = f"Sequence overview: sub-{subject}, {scan}"
    lines = [
        title,
        "=" * len(title),
        *sp.summary_lines(runs, rows),
        "",
        *sp.render_table(runs, rows, only_differences),
    ]
    return "\n".join(lines)


def status_text(dicom_read: bool, dicom_root: Path | None) -> str:
    if dicom_read:
        return "DICOM headers read for these runs."
    if dicom_root is None:
        return "From the NIfTI headers and BIDS sidecars (no DICOM folder chosen)."
    return "From the NIfTI headers and BIDS sidecars. Read DICOM headers to fill gaps."


def save_overview(runs: list[sp.ScanRun], text: str, path: Path) -> None:
    """The table as CSV, or the text as shown."""
    if path.suffix.lower() == ".csv":
        sp.write_csv(runs, sp.table_rows(runs), path)
    else:
        path.write_text(text + "\n", encoding="utf-8")
