# Headless TUI: run log

What was done on the branch `headless-tui` in the overnight run of 25 November 2026, the choices
made along the way, and what is blocked or not checked. The plan is in [Next steps](next-steps.md).

## Setup

| | |
|---|---|
| Place | WSL Ubuntu 26.04, the existing clone in `~/fmri_local_process` (remote: GitHub) instead of a new one in `~/src` |
| With Qt | `.venv` in the repo (Python 3.13 from `uv`): `uv pip install -e ".[gui,tui]" -r requirements-dev.txt` |
| Without Qt | `~/.venvs/fmri-noqt`, the same without PySide6 |
| Data | `safe_data_source/`: same 84,902 files and sizes as the Windows folder; only read. Results in `results/wsl/` (not in git) |

**Baseline, before any change:** 337 passed, 2 skipped (they need Rscript). `pytest` then crashes
as it exits (segmentation fault, code 139), as it does on Windows. So it isn't Windows-only. The
cause is still not known. Without Qt, 55 tests failed and 9 files couldn't be loaded.
`ruff format --check` flags `docs/app-layer-toy.md` only (the toy's page: left as it is).

## Phase A: the app/ layer

Done: PySide6 and Textual are optional installs (`[gui]`, `[tui]`); `app/` holds the events,
settings, shared state, tab list, every background job, the texts and the figures; `gui/` wraps
the jobs in QThreads and shows the rest. `processing/` is unchanged.

Choices:

- **Tab names stay in `processing/tab_guide.py`**; `app/tabs.py` re-exports them and adds the
  texts and the "not yet in the TUI" list. The review package's README (in `processing/`) needs
  the names, and `processing/` may not import `app/`.
- **Jobs report as in the toy:** a listener object for jobs with several kinds of report (C-PAC,
  MRIQC), a plain callback or return value for the others.
- **Figures:** the window shows the same PNG bytes the terminal app writes to a file. The files go
  in a folder only the user can read, removed when the app closes, as they show participants'
  brains. The First-level tab keeps its clickable canvases; the drawing itself is shared.
- **The review-package picker** (`build.ps1 -Review`) stays Qt only: it is a build tool, not a tab.
- **Existing tests:** only import paths and the places they replace functions changed
  (e.g. `preprocess.find_docker` instead of `preprocess_gui.find_docker`), plus
  `panel.work.…` for what moved into the behaviour logs' work object. No assertion was
  changed.
- **Small visible differences in the window:** the hint above "Not complete: n expected output(s)
  missing" is now in the warning colour, "Why it failed" is no longer bold, and "BIDS sessions
  renumbered" lists the numbers under its text instead of inside it.

Checked on `safe_data_source/` (offscreen): the whole conversion with defacing in Docker, run
through `ConvertJob` **without Qt installed**; then the window with every tab opened, the DICOM
volume check and a crosscheck note, with no errors.

Seen in the data, not changed:

- Three BOLD runs aren't converted: dcm2niix stops on them ("Missing images?"), because their
  series folders lack images. The DICOM volume check agrees.
- The loose series folder beside the sessions is skipped and listed on Raw DICOMs.
- Names instead of codes as IDs: one folder's `^` is removed and flagged as a possible typo, so the
  two folders become two different subjects. Twins (`12345-1` -> `12345T1`) are covered by the
  existing tests.
