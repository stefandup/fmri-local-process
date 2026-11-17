# Review packages

A review package is the toolbox with a chosen part of the data, for someone else to check the
processing: a supervisor, a collaborator, or you on another computer. They unzip it, open
`fmri_toolbox.exe`, and the toolbox opens on the packaged data. They don't need Python, and
they don't need Docker for the BIDS, crosscheck, overview, scan length, sequence and
first-level stats tabs.

## Making one

```powershell
.\build.ps1 -Review
```

A window opens to pick what goes in:

- **Data folders**: these start as the toolbox's own folders. Change any of them here.
- **Subjects, sessions and scans**: tick what to share. Ticking a subject ticks all its scans,
  and you can untick single sessions or scans. Each scan shows its file count and size. A
  task's files (bold, sbref, events, physio) go in together.
- **C-PAC outputs**: these come from the shared sessions and tasks only, sorted by kind, with
  the size of each: preprocessed BOLD (first-level stats need it), QC figures and values,
  motion and nuisance, anatomical outputs, other outputs, C-PAC logs, and saved first-level
  maps.
- **Behavioural logs**: the logs of the shared subjects, found by their corrected ID. Logs of
  tasks you share are ticked for you.
- **Options**:
    - **Include the raw DICOMs of the shared scans** is off by default. It adds only the
      series the shared scans were converted from, never other scans or sessions. You are
      asked to confirm, because DICOM headers can hold identifying details.
    - **Include the atlases**, so first-level stats work without Docker.
    - **View only** (on by default): the app hides Setup & Convert and Preprocess, and
      locks converting, trimming, deleting and saving log corrections. Crosscheck marks
      and notes still work.
    - **Also make a .zip**.

The line at the bottom adds up what the package will hold. **Build package** saves the
choices to `build_output\review\plan.json`, builds `fmri_toolbox.exe` (a few minutes) and
assembles `build_output\review\<name>\` and `<name>.zip`. The next `-Review` starts from the
last choices. `.\build.ps1 -Review -Plan <plan.json>` rebuilds from a saved plan without the
window.

## What is left out

Only what you tick goes in. The records that come with BIDS are cut down so they say nothing
about anyone else:

- `participants.tsv` keeps only the shared subjects.
- `sub-<id>_sessions.tsv` and `*_scans.tsv` keep only the shared sessions and scans.
- In `code/`, the expected lengths, crosscheck settings and scan rules go in whole.
- Session notes, crosscheck decisions, the action log and the ID / date / log corrections keep
  only the entries about shared sessions: those naming a shared session, its DICOM folder or a
  shared log, and nothing that isn't shared.
- Any other file in `code/` stays home.

## Inside the package

```text
<name>/
  fmri_toolbox.exe, _internal/     the app
  review_package.json              what's in it, and where
  README_REVIEW.txt
  review_data/bids|output|behav|dicom/   the data, laid out as in the original folders
  review_data/atlases/             the first-level atlases
  review_data/settings/            the app's settings
```

The app sees `review_package.json` beside it and starts on the folders in `review_data`. The
reviewer can choose other folders on the Setup tab. Those choices are kept in the package's
own settings, not on the computer, and are used while the folders exist. Moved to another
computer, it falls back to its own data. Anything the reviewer changes (trims, crosscheck
decisions, notes) changes the copy, never your data.
