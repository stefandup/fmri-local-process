"""Find the C-PAC outputs worth previewing per subject: its QC figures and motion summaries.

C-PAC writes these next to the NIfTI outputs, named with BIDS-style entities, e.g.
OUTPUT/output/pipeline_<name>/sub-<s>/ses-<n>/func/
    sub-<s>_ses-<n>_task-<t>[_reg-<k>]_desc-<what>_quality.png
    sub-<s>_ses-<n>_task-<t>_desc-summary_motion.tsv
Everything here is plain globbing and name parsing; nothing opens an image.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

# Readable names for the figures C-PAC's preproc pipeline is known to write; any other
# desc-*_quality.png still shows up, under its raw desc label.
FIGURE_NAMES = {
    "framewiseDisplacementJenkinsonPlot": "Framewise displacement (Jenkinson)",
    "framewiseDisplacementPowerPlot": "Framewise displacement (Power)",
    "movementParametersRot": "Motion: rotation",
    "movementParametersTrans": "Motion: translation",
    "boldSnrAxial": "BOLD SNR (axial)",
    "boldSnrSagittal": "BOLD SNR (sagittal)",
    "boldSnrHist": "BOLD SNR histogram",
}
ENTITY = re.compile(r"([a-zA-Z]+)-([a-zA-Z0-9]+)")


def bids_entities(name: str) -> dict[str, str]:
    """'sub-1_ses-2_task-Rest_desc-x_quality.png' -> {'sub': '1', 'ses': '2', ...}."""
    stem = name.split(".", 1)[0]
    matches = (ENTITY.fullmatch(part) for part in stem.split("_"))
    return {match.group(1): match.group(2) for match in matches if match}


@dataclass(frozen=True)
class QcFigure:
    path: Path
    session: str
    task: str
    desc: str
    reg: str = ""

    @property
    def label(self) -> str:
        """'ses-1 Rest: Motion: rotation', with ' (reg-1)' for per-regressor figures."""
        name = FIGURE_NAMES.get(self.desc, self.desc)
        reg = f" (reg-{self.reg})" if self.reg else ""
        return f"ses-{self.session} {self.task}: {name}{reg}"


def subject_files(output_root: Path, subject: str, pattern: str) -> list[Path]:
    """This subject's files matching pattern, under every pipeline_*/sub-<s>/ses-*/<kind>/."""
    return sorted((output_root / "output").glob(f"pipeline_*/sub-{subject}/ses-*/*/{pattern}"))


def matches_task(path: Path, task: str | None) -> bool:
    return task is None or f"_task-{task}_" in path.name


def qc_figures(output_root: Path, subject: str, task: str | None = None) -> list[QcFigure]:
    """The QC figures C-PAC drew for this subject (of task), in session/task/name order."""
    figures = []
    for png in subject_files(output_root, subject, "*_quality.png"):
        if not matches_task(png, task):
            continue
        entities = bids_entities(png.name)
        figures.append(
            QcFigure(
                path=png,
                session=entities.get("ses", "?"),
                task=entities.get("task", png.parent.name),  # anat figures have no task
                desc=entities.get("desc", png.stem),
                reg=entities.get("reg", ""),
            )
        )
    return figures


def motion_summaries(output_root: Path, subject: str, task: str | None = None) -> list[Path]:
    """C-PAC's per-run motion summary tables (mean FD, max motion, ...) for this subject."""
    return [
        tsv
        for tsv in subject_files(output_root, subject, "*_desc-summary_motion.tsv")
        if matches_task(tsv, task)
    ]


def read_tsv(path: Path) -> tuple[list[str], list[list[str]]]:
    """A small TSV as (header, rows); an empty file gives ([], [])."""
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.reader(handle, delimiter="\t") if row]
    return (rows[0], rows[1:]) if rows else ([], [])
