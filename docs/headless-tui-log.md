# Headless TUI: run log

What was done on the branch `headless-tui` in the overnight run of 25 November 2026, the choices
made along the way, and what is blocked or not checked. The plan is in [Next steps](next-steps.md).

## Setup

| | |
|---|---|
| Place | WSL Ubuntu 26.04, the existing clone in `~/fmri_local_process` (remote: GitHub) instead of a new one in `~/src` |
| With Qt | `.venv` in the repo: `pip install -e ".[gui]" -r requirements-dev.txt` (first made with `uv`, remade as a plain venv with pip; the Python 3.13 itself came from `uv`, as Ubuntu 26.04 has only 3.14) |
| Without Qt | `~/.venvs/fmri-noqt`, the same without PySide6 |
| Data | `safe_data_source/`: same 84,902 files and sizes as the Windows folder; only read. Results in `results/wsl/` (not in git) |

**Baseline, before any change:** 337 passed, 2 skipped (they need Rscript). `pytest` then crashes
as it exits (segmentation fault, code 139), as it does on Windows. So it isn't Windows-only. The
cause is still not known. Without Qt, 55 tests failed and 9 files couldn't be loaded.
`ruff format --check` flags `docs/app-layer-toy.md` only (the toy's page: left as it is).

## Phase A: the app/ layer

Done: PySide6 and Textual are optional installs (`[gui]`, `[tui]`; later only `[gui]`); `app/` holds the events,
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
  series folders lack images. The DICOM volume check agrees. The user's note: the second scan
  came from a new scanner software version, still being sorted out on the scanner's side.
- The loose series folder beside the sessions is skipped and listed on Raw DICOMs.
- Names instead of codes as IDs: one folder's `^` is removed and flagged as a possible typo, so the
  two folders become two different subjects. Twins (`12345-1` -> `12345T1`) are covered by the
  existing tests.

## Phase B: the terminal app

Done: `fmri_tui`, all ten tabs ([The toolbox in a terminal](tui.md)). The "not yet in the TUI" list
is empty, and the parity test keeps it honest. Nothing in it imports Qt.

Choices:

- **Jobs run on daemon threads** (not Textual workers as in the toy): Textual waits for its
  workers when it closes, which would hang for hours on C-PAC. A job that crashes shows its error
  and unlocks the tabs.
- **Pictures:** inline with sixel or kitty graphics, else only the file path, as planned. The
  rough coloured-block fallback of `textual-image` is off: a brain isn't readable in it.
- **Several rows at once:** a terminal table has no Ctrl/Shift selection, so rows are ticked with
  space.
- **Typed values are kept on Enter** (or Save / Use), not while typing as in the window.
- **Docker:** no Start / Restart buttons: `docker desktop start` doesn't exist on a server.
- **Not in the terminal app:** clicking in the brain views (type the MNI position), zoom,
  Reveal (the paths are shown), the review-package picker.
- **Settings** are in `~/.config/fmri_local_process/*.json`, not shared with the window's.
- Quitting during a conversion leaves the defacer's container to finish by itself in Docker, as
  closing the window does. The scan's temporary folder is cleared by the next conversion.

Bugs found and fixed on the way: the terminal app's MRIQC tab stored its starting values before
Docker's suggestion for a first run could be filled in.

Checked: 30 tests through Textual's test driver, also at 80 x 24 and without Qt installed; by hand
in `tmux` with no display on `safe_data_source/` (tabs, sequence overview, quitting, quitting in the
middle of a real conversion).

Not checked: inline pictures in a real Windows Terminal / WezTerm, C-PAC and MRIQC for real from the
terminal app (only with stand-ins; hours of computing), the First-level tab on real C-PAC output and
the Behaviour logs tab on real logs (neither is in `safe_data_source/`; both run on synthetic data).

## Phase C: the Linux bundle

Done: `./build.sh --tui` ([The Linux bundle](linux-bundle.md)): `fmri_tui` and four command-line tools
in one folder, without Qt, built in `rockylinux:8`; `packaging/install.sh` now installs whatever
commands its bundle holds.

Choices:

- **dcm2niix is built from source** at the pinned version. The release binary needs glibc 2.29 and
  doesn't start on Rocky 8 at all.
- **Library paths:** a PyInstaller app passes its own library folder on to the programs it starts. A
  start-up hook undoes that, so docker and mri_deface use the server's libraries.
- **UTF-8 always**, so the app also works with a plain C locale.

Checked:

- `check_bundle.sh` in clean `ubuntu:22.04`, `rockylinux:8` and `debian:12` containers without network.
- SSH into a Debian 12 container (the base of OpenMediaVault) with a throwaway key: no display, inside
  `tmux`, the connection dropped and taken up again, then a whole conversion of `safe_data_source/`.
- On WSL: the bundle finds Docker and both images, and converts with defacing in Docker (3 of 3 T1w).

Not checked: the real server (no access yet), `qsub` / Apptainer (the next branch), C-PAC and MRIQC
runs started from the bundle.

## After the run (asked for the next day)

- **What is typed is kept** (terminal app): corrections stay with their row until used or saved, notes are
  saved when their box is left, and quitting with unsaved corrections asks. Before, text typed for one
  log was lost when another was highlighted before they were used.
- **Changing tab by key didn't work** while a button, table or tick box was focused (Textual brought the old
  tab back): fixed, with a test on every tab.
- **Works like the window:** every control was compared with the window's, and a test makes the same
  corrections in both and compares the saved files. *Use* is now *Re-check*, and takes
  what is typed on both tabs, as there. Not in the terminal: *Reveal* (no file manager) and MRIQC's HTML reports.
- **One toolbox folder on the PATH**, as vr_lab_toolbox's installer: `~/.local/opt/fmri_local_process` on Linux (before:
  launchers in `~/.local/bin`, and the app beside the backups in a folder differing only in case). The
  Windows installer did this already, but had only 2 of the command-line tools; it now has all 7
  (`build.ps1`, not run yet). The terminal app is in the Linux bundle only.
- **The commands point out problems; fixing is done in the window or the terminal app.**
  `fmri_behav_check` lists the DICOM folders' problems too and applies every correction saved in the BIDS
  folder, so it agrees with the tabs. New: `fmri_bids_check` and `fmri_mriqc`. (Commands to fix things, and
  one that remembered the folders, were made and taken out again: commit `dd81424` has them.)
- **`fmri_generate_sample_data`** (vr_lab_toolbox's command line): dummy logs and DICOMs. Presentation's
  documentation doesn't fix the log's delimiter (a setting) or layout, so the yardstick is this repo's own
  reader: each dummy log has to be read as its original is, and the command says how many aren't.
- **`docker/setup_docker_image.sh`** in the bundle: the Docker part of the Windows installer. Tested
  with `--defacer pydeface`; not with `freesurfer` (5 GB download) or `local`.
- **pip instead of uv:** `python -m venv` and `pip install -e .`, as vr_lab_toolbox. Textual is in
  `requirements.txt`; only Qt stays optional (`[gui]`). The bundle is built with Rocky's own Python 3.12 and
  pip, from `specs/linux.spec`, a list of tools as in vr_lab_toolbox's `specs/toolbox.spec`.

## Still to do

1. **On Windows, before anything is merged:** update the environment (`python -m pip install -e
   ".[gui]" -r requirements-dev.txt`), run the tests, use the window on `safe_data_source/`,
   check that it remembers its folders (registry), and `build.ps1 -Full` and `-Review`.
   *26 November 2026:* environment updated and tests run: 442 passed, 7 skipped (commit
   `adcf4ae`). The run found a real Windows bug, fixed there: the scan cache could miss a DICOM
   file added to a series, as `os.scandir`'s `entry.stat()` gives the parent folder's copy of the
   times, which NTFS updates late. The Linux `bash` test is skipped on Windows (`bash` is WSL's).
   Still to do: the window on `safe_data_source/`, the registry check and the two builds.
2. **On the server:** processor, Docker access, `noexec` mounts, then install the bundle.
3. **Pictures** in Windows Terminal over SSH.
4. The crash when `pytest` exits with Qt loaded (there before; on Linux too). Narrowed down on
   26 November 2026, on Windows:
   - Not a pytest plugin: it also crashes with `--disable-plugin-autoload` (the only plugin
     installed is `platformdirs`'). Not SIP: the project uses PySide6 6.12 / `shiboken6`, not PyQt.
   - Run file by file, only `test_behav_logs_dialog.py` and `test_tui_matches_window.py` crash
     on exit (code -1073741819); the other 13 Qt test files and a bare `QApplication` don't. Both
     use the behaviour logs dialog.
   - Next: look in that dialog for a `QTimer` or `QThread` still running when the tests end
     (stop timers; `quit()` and `wait()` threads; wait for thread pools; keep the `QApplication`
     alive to the end). `qtbot.addWidget()` would need `pytest-qt`, a new dev dependency.
     Each check takes about 30 s: `python -m pytest -q tests/test_behav_logs_dialog.py`, then
     `$LASTEXITCODE`.
5. `tests/test_philani_bids.py` (from before) uses a first name as an example ID that also occurs in
   the cleared data's folder names. Harmless if meant, worth a look as the repo is on GitHub.

One slip to know about: Phase A was committed and pushed in the same command as its last checks, before
their result had been read. Looked into straight after: the 365 tests had passed, and pyright's five
errors were in a scratch script outside git. From then on, a script had to pass before each commit.
