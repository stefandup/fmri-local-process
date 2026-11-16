"""TEMPORARY: check the Python behavioural port against the original Rmds.

Runs the R code of philani_scripts' *_regressors.Rmd with Rscript (no rmarkdown/pandoc
needed) on the same log and compares the
"_withrating" CSV it writes with the Python EventTable. Remove once parity is confirmed.

The R output is converted to the Python conventions before comparing: times / 10 000
(0.1 ms -> s), and both sides are compared relative to their first event, because Python
measures onsets from the first scanner pulse and R from the first event.
"""

import csv
import math
import re
import subprocess
from pathlib import Path

from fmri_local_process.processing.behav_logs import (
    MISSING,
    TIME_UNITS_PER_SECOND,
    BehavTask,
    EventTable,
)

RMD_FILES = {
    "Reappraisal": "cogreappraisal_regressors.Rmd",
    "Matching": "emolabeling_regressors.Rmd",
}
TOLERANCE = 1e-6


def _r_string(value: Path | str) -> str:
    text = value.as_posix() if isinstance(value, Path) else value
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def rmd_r_code(rmd_text: str) -> str:
    """The R code of every ```{r ...} chunk in an Rmd, in order.

    Running the code directly (instead of rmarkdown::render) means no rmarkdown/pandoc is
    needed -- only R and the packages the chunks load.
    """
    chunks = re.findall(r"^```\{r[^}]*\}[^\n]*\n(.*?)^```", rmd_text, flags=re.M | re.S)
    if not chunks:
        raise ValueError("no ```{r} chunks in the Rmd")
    return "\n".join(chunks)


def render_r_events(
    rscript: Path, rmd_dir: Path, task: BehavTask, log_file: Path, subject_id: str, work_dir: Path
) -> Path:
    """Run the task's Rmd code on log_file; returns the CSV it wrote (output stays in work_dir)."""
    rmd = rmd_dir / RMD_FILES[task.task]
    if not rmd.is_file():
        raise FileNotFoundError(f"{rmd} not found")
    params = (
        f"params <- list(logfile = {_r_string(log_file.resolve())}, "
        f"participant_id = {_r_string(subject_id)}, output_dir = {_r_string(work_dir)})\n"
    )
    script = work_dir / f"{rmd.stem}.R"
    script.write_text(params + rmd_r_code(rmd.read_text(encoding="utf-8")), encoding="utf-8")
    subprocess.run(
        [str(rscript), "--vanilla", str(script)],
        check=True,
        capture_output=True,
        text=True,
        cwd=work_dir,
    )
    csv_path = work_dir / f"sub-{subject_id}_task-{task.task}_events_withrating.csv"
    if not csv_path.is_file():
        raise FileNotFoundError(f"Rscript finished but wrote no {csv_path.name}")
    return csv_path


def _same_value(py: str | float | None, r: str) -> bool:
    r_missing = r.strip() in ("", "NA", "NaN", MISSING)
    if py is None or (isinstance(py, float) and math.isnan(py)):
        return r_missing
    if r_missing:
        return False
    try:
        return math.isclose(float(py), float(r), abs_tol=TOLERANCE)
    except ValueError:
        return str(py).strip() == r.strip()


def compare_with_r(table: EventTable, r_csv: Path) -> list[str]:
    """Differences between the Python table and the R CSV; empty list = identical."""
    with r_csv.open(encoding="utf-8") as f:
        r_rows = list(csv.DictReader(f))
    if len(r_rows) != len(table.onset):
        return [f"row count: python {len(table.onset)}, R {len(r_rows)}"]

    py_start = table.onset[0] if table.onset else 0.0
    r_start = float(r_rows[0]["onset"]) / TIME_UNITS_PER_SECOND if r_rows else 0.0
    diffs = []
    for i, r in enumerate(r_rows):
        py_values: dict[str, str | float | None] = {
            "trial_type": table.trial_type[i],
            "onset": table.onset[i] - py_start,
            "duration": table.duration[i],
            table.modulator: table.modulator_values[i],
        }
        r_values = {
            "trial_type": r["trial_type"],
            "onset": str(float(r["onset"]) / TIME_UNITS_PER_SECOND - r_start),
            "duration": _seconds(r["duration"]),
            table.modulator: r.get(table.modulator, ""),
        }
        for column, py_value in py_values.items():
            if not _same_value(py_value, r_values[column]):
                diffs.append(f"row {i + 1} {column}: python {py_value!r}, R {r_values[column]!r}")
    return diffs


def _seconds(raw: str) -> str:
    try:
        return str(float(raw) / TIME_UNITS_PER_SECOND)
    except ValueError:
        return raw
