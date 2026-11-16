# Philani DICOM → BIDS conversion

Two commands, both thin wrappers around `src/fmri_local_process/processing/`:

| Command | What it does |
|---|---|
| `fmri_convert_to_bids` | Converts a folder of DICOM sessions into a BIDS dataset, including behavioural `events.tsv` files when given `--behav-logs`. |
| `fmri_behav_events` | Batch-converts behavioural `.log` files (one file, or a whole folder) into `events.tsv` files using the same code. `--compare` runs the original R script on each log and reports any differences. |
| `fmri_behav_check` | Read-only report: which log matches which scan session (by ID and date), which don't and why, which logs are incomplete, and what trial types and columns the logs contain. |

This is a Python port of `philani_scripts/philaniToBIDS.sh` and the behavioural Rmds in
`philani_scripts/behavioural_processing_r_scripts/`. Neither command needs R, `jq` or bash.
## Defacing

T1w images are defaced with the first of these that is available:

1. `--freesurfer <folder>`: a FreeSurfer home, or a folder holding `mri_deface`,
   `talairach_mixed_with_skull.gca` and `face.gca`;
2. `$FREESURFER_HOME`, if it holds those files;
3. the `philani_fmri_process:latest` Docker image the installer builds on top of C-PAC, with
   the defacer picked during install: FreeSurfer `mri_deface` (copied out of the official
   FreeSurfer image, about 5 GB extra to download once), your own `mri_deface` files, or
   pydeface.

`mri_deface` is a Linux program, so on Windows options 1 and 2 run inside the C-PAC
container with the folder mounted. FreeSurfer has taken the standalone `mri_deface` download
offline, which is why the installer gets it from the FreeSurfer Docker image. To change the
defacer later, re-run the installer, or run
`powershell -ExecutionPolicy Bypass -File <install dir>\docker\setup_docker_image.ps1 -Defacer <freesurfer|local|pydeface|none>`
(`local` also needs `-DefaceFiles <folder>`). `--no-deface` skips defacing. The sidecar's
`Notes` names the tool that was used.

## Input

```
DICOM_ROOT/
  20240131_<x>_<subject id>[_...]/   one folder per scan session; date is YYYYMMDD
    .../<n>_rest_AP/                 series folders, found anywhere inside
  trash/                             ignored
BEHAV_LOGS/                          only with --behav-logs; searched recursively
  .../cogreappraisal/.../<subject id>-*.log
  .../emolabeling/.../<subject id>-*.log
```

Each log's task comes from `cogreappraisal` or `emolabeling` appearing anywhere in its path
below `BEHAV_LOGS`, as a folder name or in the file name. Logs with neither are ignored. The
subject ID is the part of the name before `-<task>`. Logs are named
`<ID>-<task>_scan-YYYYMMDD-HHMMSS-<unique id>.log`, and the date (and time) comes from the
`_scan-` part. For other names it is the first 8-digit `YYYYMMDD`, else the date on the log's
`Logfile written - MM/DD/YYYY` line. `BEHAV_LOGS` can be
`DICOM_ROOT` itself if the logs live alongside the DICOMs.

Folders that are empty or don't start with a valid `YYYYMMDD_` date are skipped with a
warning. The folder's second part, when it is `HHMM`, is shown as the scan time.

**Subject IDs** (folders and logs alike):

- A twin's dash between digits becomes `T`: `12345-1` is `sub-12345T1`, `12345-2` is
  `sub-12345T2`.
- Any other character BIDS doesn't allow (anything not a letter or digit, e.g. `^` or a
  space) is dropped, with a warning. A dropped character may hide a typo, so the crosscheck
  flags it.
- A wrong ID is corrected in the crosscheck (**IDs & behaviour logs...**, see below). Raw
  folders and logs are never renamed.

To convert a single session folder that doesn't follow these naming rules, pass the folder
itself as `DICOM_ROOT` with `--subject <id>`: it becomes `sub-<id>/ses-1`.

A series folder that is a copy of another, meaning the same folder name with the same file
names (e.g. a scanner export's extra `Untitled/` subfolder), is converted once rather than
as a repeated run.

## Output

```
BIDS_ROOT/
  dataset_description.json
  sub-<id>/ses-<n>/anat/sub-<id>_ses-<n>_T1w.nii.gz (+ .json, defaced)
  sub-<id>/ses-<n>/func/sub-<id>_ses-<n>_task-<task>_bold.nii.gz (+ .json)
  sub-<id>/ses-<n>/func/sub-<id>_ses-<n>_task-<task>_events.tsv (+ .json: SourceLog)
  sub-<id>/sub-<id>_sessions.tsv     each ses-<n>: scan date/time and DICOM source folder
```

- **Sessions.** Every session is converted. `ses-1`, `ses-2`, … follow the calendar order of
  each subject's scan dates, and every subject gets `ses-1`, even with a single session.
  `sub-<id>_sessions.tsv` records which DICOM folder each session came from, with its scan
  date and time. If an earlier session turns up later, the converted session folders (and
  the files in them) are renamed so the numbers stay in date order, and the summary lists
  them as **RENUMBERED**. Outputs made from the old numbers elsewhere (e.g. C-PAC) are not
  renamed.
- **Scans.** Each `ScanRule` in `philani_bids.py` maps a series folder pattern to a BIDS name:
  T1w, Reappraisal, Matching, Rest, Peer and Checkerboard.
- **Events** (BIDS conventions):
    - `onset` and `duration` are in **seconds**. Presentation logs time in units of 0.1 ms.
    - `onset` is measured from the **first scanner pulse** (the first volume). This assumes
      the first logged pulse matches the first volume saved in the BOLD file.
    - There is one file per run, with the behavioural modulator as an extra column: `rating`
      for Reappraisal, `sub_acc` (block accuracy) for Matching.

### When there is a choice to make

Everything is converted straight into its session; you choose in the crosscheck afterwards.

| Situation | What the converter does |
|---|---|
| Several series of the same scan in one session | Converts each as `..._run-<k>_<suffix>` in that session. In the crosscheck, **Keep selected run** gives the chosen one the run-less name and deletes the others; re-runs then leave it alone. Until then C-PAC would preprocess every run. |
| A log's date matches one of the subject's sessions with that task | Puts its events in that session. |
| One log and one session with that task, but the dates differ | Puts it in that session anyway (if there is no other session, it's that one) and the crosscheck flags it ⚑. |
| Several logs for one session, a date matching no session, or a subject with no scan session | Doesn't convert it; the summary lists it as **NOT PLACED**. Choose a log, correct its date or ID in **IDs & behaviour logs...**, then convert again. |

If a different log is chosen later, its events replace the old ones on the next conversion.
Older versions staged these in `crosscheck_pending/`; that folder is removed on the next
conversion (everything in it is made from the raw data).

### Safe writing and disk space

Each scan is converted, and a T1w defaced, in a temporary `.converting/` folder inside the
BIDS folder. It moves into its session only once that has all worked, and the temporary
folder is then removed. If a conversion stops halfway (a crash, a power cut, a full disk), the
session never holds a half-written scan or a T1w that wasn't defaced: the next conversion does
that scan again. Before each session the converter checks that the BIDS drive has room for
that session's DICOM folder plus 1 GB. If not, it stops with a message; the sessions already
converted are kept.

### Incomplete DICOM series

dcm2niix refuses a whole series if a single file is cut short, which happens when a copy
didn't finish. When it does, the converter converts the files that are intact (any file well
under the largest in the series is left out) and lists the series as **INCOMPLETE** in the
summary. Nothing about this is written into the BIDS folder. The crosscheck tool finds the
short scan from the data itself.

To fix it, re-copy the raw session, delete the short scan in the crosscheck tool, and
convert again.

### Crosscheck tool: `fmri_crosscheck`

A PySide6 window, modelled on the vr_lab_toolbox crosscheck tools. It reads only the BIDS
folder. The only changes it makes there are the ones you click.

| Icon | Meaning |
|---|---|
| ⚠ | A scan converted as several runs: keep one (the others are deleted) |
| ❗ | Short: fewer volumes than the scan's expected length (bytes for a 3D image: smaller than in other sessions), or an `events.tsv` with no events |
| ✂ | Long: more volumes than the expected length (the scan ran on after the task): trim it |
| ○ | Missing: a NIfTI without its sidecar, a task bold without its `events.tsv`, or a scan most other sessions have |
| ⚑ | Events from a log dated other than the scan: check it's the right log |
| ⊕ | The subject has more sessions than **Sessions per subject**: is one a dud visit? |

The session list shows each session's **ID**, **Date** (scan date and time, from
`sessions.tsv`; hover it for the DICOM folder), **Session**, number of files, problems and
your **Note**.

**Sessions per subject** (beside the buttons, default 1) is how many sessions this study
expects; set it to 2 for a two-visit study. It is saved in `code/crosscheck_settings.json`.
A subject with more sessions gets ⊕ on each of them, with all its sessions and dates listed,
so a dud visit, or one visit split over two days, stands out.

**Ticking sessions.** Tick the box beside an ID to act on several sessions at once; the
**Ticked sessions** bar above the detail pane then offers:

- **Note...**: give them all the same note (empty clears it).
- **Delete N session(s)...**: delete the session folders, after asking you to confirm.

**Select none** clears the ticks. A single session has the same **Note** box and
**Delete session...** button at the top of its detail pane.

**Deleting a session** removes its `ses-<n>` folder and lists its DICOM folder in
`code/excluded_sessions.json`, so later conversions leave it out instead of bringing it back.
Click **Convert / update BIDS** afterwards: the subject's remaining sessions are renumbered
in date order, and the summary lists the folder as **LEFT OUT**. To convert it again, remove
its line from `code/excluded_sessions.json`.

Notes are saved in `code/session_notes.json` against the DICOM folder, so they stay with the
right session when sessions are renumbered. **Export CSV...** writes every session with its
date, DICOM folder, note and problems (one row per problem, `ok` if none) for QC meetings or
handing over.

**Action log.** Every change made here is added to `code/crosscheck_actions.tsv` in the BIDS
folder, with the time and the computer login: runs kept, files and sessions deleted,
trims, notes, ID and log corrections, expected lengths and sessions, and sessions the
converter renumbered.

Every file also shows its volume count and duration.

**Scan lengths & trimming.** A BOLD scan's expected length is the median volume count
of all its runs. (It's the upper median, so it is always a length some run has, and with
only two runs it's the longer one.) A scan with a single run in the whole folder (e.g. one
pilot) is compared with the study's usual length instead, shown as "study default": Matching
495, Reappraisal 500, Rest 495 and Peer 140 volumes. **Scan lengths & trimming...** lists each BOLD scan with
its runs, the volume counts found, the expected length (shown as "median (N)"), the minutes
that makes, and how many runs are too long. Type
another number to set it; it is remembered in `code/expected_volumes.json` in the BIDS
folder. Step the box down to the bottom ("median (N)") to follow the median again. A run
longer than its expected length gets **Trim to N...** in its session. **Save and trim N long
run(s)...** in the same window saves the lengths and trims them all, after asking you to
confirm. Trimming keeps the first N volumes, records the original count in the sidecar
(`TrimmedFromVolumes`), and refuses if the run's events would be cut off. The raw DICOMs
keep every volume: delete the file and convert again to undo. Trimming happens here, at
crosscheck, never during preprocessing. A single-run scan without a study default (e.g.
Checkerboard, or the T1w) can't be marked short automatically, so check those numbers by
eye. **Convert / update BIDS** runs the converter from the window, with a progress bar (one step per session, plus one per subject for its behavioural logs) and the step it is on. Deleted files are recreated
from the raw data.

### Behaviour logs: wrong IDs and dates

Logs are often saved under a mistyped subject ID or on the wrong date. **IDs & behaviour
logs...** in the crosscheck window (it needs all three folders set) has two tabs.

**Scan sessions** lists every DICOM session folder with:

- when it was scanned (date and time from the folder name), which tells twins scanned on the
  same day apart;
- the ID as written in the folder, and the ID it resolves to;
- a **Corrected ID** box;
- its BIDS session, its task scans, and which logs matched it (with the log's time), or
  which task has no log.

A correction is saved in `code/dicom_id_corrections.json`, keyed by folder name. Sessions
already converted under the old ID stay in the BIDS folder until you remove them (or
rebuild with `--reset`).

**Behaviour logs** lists every log (ID as written and as resolved, its date and time) with:

| Match | Meaning |
|---|---|
| matched | ID and date match one session with that task scan |
| date mismatch | the ID has scans, but not on this date; the scan dates are listed |
| unknown ID | no session has this ID; same-day sessions that still lack this task's log are suggested |
| duplicate | several logs for one session and task on the same day: pick one (below) |
| excluded | marked **error** or **not used**: left out of BIDS |
| no task scan | a session on that date, but without this task's scan |
| no date | neither the file name nor the log header has a date |

It also shows each log's state: **complete**, **aborted** (it ends before the thank-you
screen), or **unreadable**. Notes flag logs with no scanner pulses, or a column count other
than the one the R script hard-codes. Below the table it lists the trial types and column
layouts found per task.

Type a corrected ID or date (`YYYY-MM-DD`) and press **Re-check** to see the effect.

Select a log to see, under **Selected log**, the events it produces (onset, duration,
trial type and modulator) and whether they fit the converted BOLD run (volumes × TR). A
summary beside it lists each trial type with its event count and total time, then the
behavioural column per trial type. Ratings are ordinal, so they are summarised as how often
each value occurs, the median and the range, never a mean. Accuracy gets median and range.
Rows of a rated trial type without a value are counted as missing. Then
record a verdict, with an optional note:

- **Mark OK**: the issues are acceptable; the log is used.
- **Mark error**: unusable; the log is left out of BIDS.
- **Use this one**: of several logs for a subject, task and day (e.g. a restarted run), put
  this one into BIDS. The others are marked **not used** (one already marked error keeps
  that verdict).
- **Clear verdict**.

**Needs review only** hides logs that are fine or already have a verdict. **Save**
writes the corrections and verdicts to `code/behav_corrections.json` in the BIDS folder, and **Convert /
update BIDS** applies them. Raw logs are never renamed. The events sidecar records the
correction (`SourceLogCorrectedID`, `SourceLogCorrectedDate`).
`fmri_behav_check BEHAV_LOGS DICOM_ROOT --bids BIDS_ROOT` prints the same report in the
terminal.

Every subject is expected to have both a Reappraisal log and a Matching log. A missing one is
logged, listed as **MISSING** in the summary, and conversion carries on. It does not affect
the exit code.

### Re-running

Re-running is safe:

- Files that already exist are not converted again.
- T1w images are defaced once; the deface note in the JSON sidecar marks them as done.
- A session folder over 1 GB counts as done and is skipped, unless one of its expected scans
  is missing (e.g. deleted at crosscheck so it gets converted again).

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

# 3. Behavioural logs -> events.tsv (a single .log, or a folder searched recursively)
fmri_behav_events <BEHAV_LOGS> results\behav
# expect: a progress bar, then a summary table (written / skipped / failed);
#         results\behav\sub-<id>_task-<task>[_run-<n>]_events.tsv

# 4. One participant, redone and checked against the original R
#    (needs Rscript on PATH with tidyverse and stringr; no rmarkdown/pandoc needed)
$env:PHILANI_RMD_DIR = "behavioural_processing_r_scripts"
fmri_behav_events <BEHAV_LOGS> results\behav --subject_id P001 --rerun --compare
# expect: "R agrees" in the summary, or one DIFF line per mismatching cell (exit code 1)

# 5. Full conversion on a small DICOM folder (--no-deface: skip the Docker defacer here)
fmri_convert_to_bids <DICOM_ROOT> results\bids --behav-logs <BEHAV_LOGS> --no-deface -y
# expect: a summary table (converted / runs to pick / logs not placed / renumbered /
#         skipped / failed), and results\bids\sub-*\ses-*\{anat,func}
```

`fmri_behav_events` works like the vr_lab_toolbox batch tools:
- `--subject_id` limits it to one participant.
- An `events.tsv` that already exists is skipped unless you pass `--rerun`.
- A log that fails is reported and the rest still run.
- The exit code is 1 if any log failed or differed from R.
- The task comes from `cogreappraisal` or `emolabeling` in the log's file or folder name.
  Pass `--task` to override it.

`--compare` converts R's output to the Python conventions before comparing. Times are
divided by 10 000 (0.1 ms to seconds) and both sides are aligned at their first row, because
R measures onsets from the first event rather than the first pulse. Known intentional
differences are listed at the top of `processing/behav_logs.py`, such as block accuracy for
runs with 10 or more blocks.
