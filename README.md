# fmri_local_process

> **Status:** step 0, the Philani DICOM → BIDS conversion, works and is tested on synthetic
> data. Everything from "The idea" onwards is still the plan.

## 0. DICOM → BIDS conversion (built)

```powershell
fmri_convert_to_bids <DICOM_ROOT> <BIDS_ROOT> --behav-logs <BEHAV_LOGS> [--no-deface | --freesurfer DIR] [--reset]
fmri_behav_events <log file | folder> <out dir> [--subject_id P001] [--rerun] [--compare]
```

- Every scan session becomes `sub-<id>/ses-<n>/`, in calendar order of the scan date.
- Behavioural logs become BIDS `events.tsv` files: seconds, onsets from the first scanner
  pulse. This is a Python port of the Rmds, so R isn't needed.
- Repeated runs, and logs that can't be tied to one session, are staged in
  `crosscheck_pending/` for crosscheck instead of being guessed.
- An old flat-layout BIDS folder (from `philaniToBIDS.sh`) needs `--reset`.

Full details and a step-by-step "test it yourself" plan:
[docs/bids-conversion.md](docs/bids-conversion.md).

### Next steps

1. **Check the port against R on real logs.** Run `fmri_behav_events --compare` on a few
   complete and aborted logs of both tasks. Then drop the `--compare` flag and
   `processing/behav_compare.py`.
2. **Try the crosscheck tool** (`fmri_crosscheck`) on a full BIDS folder. It replaces
   `philaniBIDSspreadsheet.py`. Non-chosen runs are deleted (they can be recreated from
   raw) rather than moved to `trash/`, and no `selected_runs.json` is written.
3. **Check the dummy-scan assumption.** Events onsets assume the first logged pulse is the
   first volume saved in the BOLD file.
4. **Packaging.** Add `fmri_behav_events` to the PyInstaller and installer builds if it
   should ship. It is only on PATH via `pip install -e .` for now.
5. **Linux build.** Run `./build.sh` and `packaging/install.sh` on Ubuntu.

## 1a. Preprocessing via C-PAC in Docker (built, not yet run on real data)

```powershell
fmri_preprocess <BIDS_ROOT> <OUTPUT> [--subject_id P001] [--task Rest] [--rerun] [--n-cpus 4] [--mem-gb 8]
```

- Runs C-PAC's standard `preproc` preset (`fcpindi/c-pac:release-v1.8.7`), one container
  per subject. This answers the "Windows-native vs container" question below for now:
  FSL/AFNI/ANTs run inside the container.
- Only `sub-<id>/` and `dataset_description.json` are mounted, so `crosscheck_pending/` is
  never used as input.
- Output goes to `OUTPUT/output/pipeline_cpac_preproc/` and logs to `OUTPUT/log/`. A subject
  is skipped when every session already has a `desc-preproc_bold`, unless `--rerun` is passed.
- `fmri_preprocess_gui` does the same from a window (vr_lab_toolbox process-results
  layout): pick the BIDS and output folders, task, CPUs and memory, then click "Preprocess".
  It shows each subject's status, its preprocessed BOLD files, and the end of its C-PAC log.

## The idea

A small, locally installable toolbox for fMRI that does three things:

1. **Process**: an installable CLI for basic preprocessing.
2. **QC**: quality-control reports for each processed run.
3. **View**: a GUI that opens the processed output and shows the timeseries for a selected
   brain region.

```
raw BOLD (BIDS) ──► fmri_process (CLI) ──► derivatives/ ──► QC reports
                                               │
                                               └──► fmri_viewer (GUI): pick region → timeseries
```

## 1. Processing CLI

- Built with `click` (the established stack, see [AI Style Guide](docs/ai-style-guide.md)),
  installed via `pyproject.toml` `[project.scripts]` so `fmri_process` lands on PATH.
- Reuse the **Python tools [C-PAC](https://github.com/FCP-INDI/C-PAC) is built on** for
  basic preprocessing, QC and display, rather than C-PAC itself:
  - `nibabel`: NIfTI I/O
  - `nipype`: workflow/interface layer (C-PAC's pipeline engine)
  - `nilearn`: masking, smoothing, filtering, confound regression, plotting
  - `pybids`: finding inputs in a BIDS dataset
  - `numpy` / `scipy` / `pandas` / `matplotlib`
- Candidate "basic" steps: motion parameters and framewise displacement, slice-timing
  correction, spatial smoothing, temporal filtering, nuisance regression, and parcellation
  into regional timeseries with an atlas.
- Keep the CLI thin: it reads arguments and calls into `processing/`.

## 2. QC

- Use **[HALFpipe](https://github.com/HALFpipe/HALFpipe)'s QC tools** (its per-subject HTML
  QC reports and rating workflow) as the model, and reuse them where possible.
- C-PAC's own `CPAC/qc` module (mosaic/montage plots, motion plots, `qcmetrics`) is the
  fallback or complement.

## 3. Region timeseries GUI

- `PySide6` app over a processed output folder.
- Pick subject/run → pick a region (an atlas label, or click on a slice) → plot that
  region's mean timeseries, optionally with motion/FD overlaid.
- Read-only: the GUI only displays processed output and never re-runs processing.

## Open questions

- **Windows-native vs WSL/container.** C-PAC (via nipype) delegates its heavy steps
  (motion correction, registration, segmentation) to FSL / AFNI / ANTs, which don't run
  natively on Windows. Options: (a) a pure-Python subset (nilearn/nibabel, possibly
  `antspyx`) that runs natively, (b) run those steps in WSL or Docker, or (c) start from
  already-preprocessed input (e.g. fMRIPrep output) and do only the "basic" steps locally.
- **HALFpipe QC reuse.** HALFpipe ships as a Linux container. Decide whether to depend on
  it, vendor just the report/rating pieces, or reimplement the same layout. Check licences
  (C-PAC is LGPL-3.0) before vendoring code.
- **Output layout.** Probably BIDS-derivatives style, so the GUI and QC can find things
  by convention.
- **Atlas** for region selection (e.g. Schaefer, Harvard-Oxford via `nilearn.datasets`).

## Layout

```
src/fmri_local_process/
  cli/          # thin click entry points (convert_to_bids, behav_events)
  processing/   # the actual logic (philani_bids, behav_logs, dcm2niix, deface, ...)
  gui/          # PySide6 timeseries viewer (planned)
tests/
```

## Development

- Python 3.13, `.venv` in the repo root:
  `.venv\Scripts\python -m pip install -r requirements.txt -r requirements-dev.txt`
- Style: [Golden Rules](docs/golden-rules.md) and [AI Style Guide](docs/ai-style-guide.md).
  Agent rules are in [AGENTS.md](AGENTS.md).
- Build: `.\build.ps1 -Exe | -Full | -Inno` (all output goes to `build_output/`).
- Docs: `mkdocs serve`
