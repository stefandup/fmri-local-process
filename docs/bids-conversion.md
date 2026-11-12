# Philani DICOM → BIDS conversion

Two commands, both thin wrappers around `src/fmri_local_process/processing/`:

| Command | What it does |
|---|---|
| `fmri_convert_to_bids` | Converts a folder of DICOM sessions into a BIDS dataset, including behavioural `events.tsv` files when given `--behav-logs`. |
| `fmri_behav_events` | Converts **one** behavioural `.log` into an `events.tsv` using the same code. `--compare` runs the original R script on the same log and reports any differences. |

This is a Python port of `philani_scripts/philaniToBIDS.sh` and the behavioural Rmds in
`philani_scripts/behavioural_processing_r_scripts/`. Neither command needs R, `jq` or bash.
Defacing still needs FreeSurfer (`mri_deface`), which runs on Linux/macOS only. On Windows,
pass `--no-deface`.

## Input

```
DICOM_ROOT/
  20240131_<x>_<subject id>[_...]/   one folder per scan session; date is YYYYMMDD
    .../<n>_rest_AP/                 series folders, found anywhere inside
  trash/                             ignored
BEHAV_LOGS/                          only with --behav-logs
  cogreappraisal/<subject id>-*.log
  emolabeling/<subject id>-*.log
```

Folders that are empty or don't start with a valid `YYYYMMDD_` date are skipped with a
warning.

## Output

```
BIDS_ROOT/
  dataset_description.json
  .bidsignore                        lists crosscheck_pending/
  sub-<id>/ses-<n>/anat/sub-<id>_ses-<n>_T1w.nii.gz (+ .json, defaced)
  sub-<id>/ses-<n>/func/sub-<id>_ses-<n>_task-<task>_bold.nii.gz (+ .json)
  sub-<id>/ses-<n>/func/sub-<id>_ses-<n>_task-<task>_events.tsv (+ .json: SourceLog)
  crosscheck_pending/                ambiguous items, for crosscheck (see below)
```

- **Sessions.** Every session is converted. `ses-1`, `ses-2`, … follow the calendar order of
  each subject's scan dates, and every subject gets `ses-1`, even with a single session. If an
  earlier session turns up later, the later ones are renumbered.
- **Scans.** Each `ScanRule` in `philani_bids.py` maps a series folder pattern to a BIDS name:
  T1w, Reappraisal, Matching, Rest and Peer.
- **Events** (BIDS conventions):
    - `onset` and `duration` are in **seconds**. Presentation logs time in units of 0.1 ms.
    - `onset` is measured from the **first scanner pulse** (the first volume). This assumes
      the first logged pulse matches the first volume saved in the BOLD file.
    - There is one file per run, with the behavioural modulator as an extra column: `rating`
      for Reappraisal, `sub_acc` (block accuracy) for Matching.

### Staged for crosscheck: `crosscheck_pending/`

The converter never guesses which of several candidates is the right one. It stages them all:

| Situation | Staged as |
|---|---|
| Several series of the same scan in one session | `crosscheck_pending/sub-<id>/ses-<n>/<datatype>/..._run-<k>_<suffix>` |
| Several logs for a subject + task, or a log that can't be tied to exactly one session with that task | `crosscheck_pending/sub-<id>/func/sub-<id>_task-<task>_run-<k>_events.tsv` (+ `.json` naming the source log) |

At crosscheck, put the chosen file into the main tree under its run-less name. Re-runs
then leave that item alone and don't stage it again.

A task that was scanned but has no log is reported as a **failure**, and the exit code is 1.

### Re-running

Re-running is safe:

- Files that already exist are not converted again.
- T1w images are defaced once; the deface note in the JSON sidecar marks them as done.
- A session folder over 1 GB counts as done and is skipped.

`--reset` deletes `sub-*`, `crosscheck_pending/`, `crosscheck.json` and `selected_runs.json`
first.

!!! warning "Old flat layout"
    A `BIDS_ROOT` written by `philaniToBIDS.sh`, or by an earlier version of this tool, uses
    `sub-<id>/anat` without `ses-<n>/`. Rebuild it with `--reset`.

## Testing it yourself

Run these from the repo root in PowerShell, with `.venv` active. Write output outside
`safe_data_source/`.

```powershell
# 1. Install the package in editable mode so both commands land on PATH
python -m pip install -e . --no-deps
# expect: "Successfully installed fmri-local-process-..."

# 2. Unit tests (synthetic data only)
python -m pytest -q
# expect: all passed

# 3. One behavioural log -> events.tsv
fmri_behav_events <path\to\P001-emolabeling.log> -o results\behav
# expect: "Wrote N events to results\behav\sub-P001_task-Matching_events.tsv"

# 4. Same, checked against the original R (needs R with rmarkdown, tidyverse, stringr)
$env:PHILANI_RMD_DIR = "C:\Users\stefan\philani_scripts\behavioural_processing_r_scripts"
fmri_behav_events <path\to\P001-emolabeling.log> -o results\behav --compare
# expect: "Python and R agree", or one DIFF line per mismatching cell (exit code 1)

# 5. Full conversion on a small DICOM folder (Windows: no FreeSurfer, so --no-deface)
fmri_convert_to_bids <DICOM_ROOT> results\bids --behav-logs <BEHAV_LOGS> --no-deface -y
# expect: a summary table (converted / staged / skipped / failed), and
#         results\bids\sub-*\ses-*\{anat,func}; anything ambiguous in crosscheck_pending\
```

`--compare` converts R's output to the Python conventions before comparing. Times are
divided by 10 000 (0.1 ms to seconds) and both sides are aligned at their first row, because
R measures onsets from the first event rather than the first pulse. Known intentional
differences are listed at the top of `processing/behav_logs.py`, such as block accuracy for
runs with 10 or more blocks.
