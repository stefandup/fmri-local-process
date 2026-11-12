# fmri_local_process

> **Status: idea / skeleton.** Nothing below is built yet. This is the pencilled-in plan to
> take forward step by step.

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

## Planned layout (not created yet)

```
src/fmri_local_process/
  cli/          # thin click entry points
  processing/   # the actual preprocessing / QC logic
  gui/          # PySide6 timeseries viewer
tests/
```

## Development

- Python 3.13, `.venv` in the repo root:
  `.venv\Scripts\python -m pip install -r requirements.txt -r requirements-dev.txt`
- Style: [Golden Rules](docs/golden-rules.md) and [AI Style Guide](docs/ai-style-guide.md).
  Agent rules are in [AGENTS.md](AGENTS.md).
- Build: `.\build.ps1 -Exe | -Full | -Inno` (all output goes to `build_output/`).
- Docs: `mkdocs serve`
