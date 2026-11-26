# fmri_local_process

## Install on a Linux server (e.g. nala)

```bash
curl -fsSLO https://github.com/stefandup/fmri-local-process/releases/latest/download/fmri_update
bash fmri_update && rm fmri_update   # then log in again
fmri_tui                             # start it (best inside tmux)
fmri_update                          # later: get the newest version
```

No sudo; it installs for your user only. You need to be in the server's `docker` group.
More: [docs/linux-bundle.md](docs/linux-bundle.md).

> **Status:** step 0, the Philani DICOM → BIDS conversion, and step 1a, C-PAC preprocessing,
> are built and tested on synthetic data. Everything from "The idea" onwards is still the
> plan. User guides: [docs/bids-conversion.md](docs/bids-conversion.md) and
> [docs/preprocessing.md](docs/preprocessing.md) (or `mkdocs serve`).

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
2. **Try the toolbox window** (`fmri_toolbox`: Setup & Convert to BIDS, ID & Behaviour
   Logs, Scan lengths & trimming, BIDS Crosscheck, Preprocess BIDS, Overview tabs) on a full
   BIDS folder. The BIDS Crosscheck and Overview tabs replace
   `philaniBIDSspreadsheet.py`. Non-chosen runs are deleted (they can be recreated from
   raw) rather than moved to `trash/`, and no `selected_runs.json` is written.
3. **Check the dummy-scan assumption.** Events onsets assume the first logged pulse is the
   first volume saved in the BOLD file.
4. **Windows build.** Run `build.ps1 -Full` and check that a new terminal finds every tool.

## Linux: one PC or server (e.g. OpenMediaVault)

The whole toolbox without a display: the terminal app `fmri_tui`, which works like the window, and
command-line tools that point out problems on their own. Needs a 64-bit Intel/AMD PC and, for C-PAC,
MRIQC and defacing, Docker. Details: [the terminal app](docs/tui.md), [the Linux
bundle](docs/linux-bundle.md).

### From this repo

```bash
python -m venv .venv                 # Python 3.12 or later
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -e .                     # the terminal app and every command; ".[gui]" adds the window
```

With the environment active, every command is on the PATH.

### As a bundle, for a server without Python

```bash
./build.sh --tui                     # in WSL or Linux, with Docker: like build.ps1 -Full
packaging/linux/check_bundle.sh      # optional: tries it in clean containers

scp build_output/installer/fmri_tui-*-linux-x86_64.tar.gz user@server:
ssh user@server
tar -xzf fmri_tui-*-linux-x86_64.tar.gz && cd fmri_tui-*/
./install.sh                         # no sudo; one toolbox folder, added to your PATH: open a new terminal
~/.local/opt/fmri_local_process/docker/setup_docker_image.sh --defacer pydeface --mriqc    # C-PAC, MRIQC, defacer (~20 GB)
```

Or build and install for your user on the server in one go (the SSH connection is checked before
the build):

```bash
./build.sh --tui --remote-install user@server    # or a Host from ~/.ssh/config
```

An upgrade, either way, replaces the earlier version: `install.sh` swaps the toolbox folder, and
`--remote-install` also removes earlier unpacked `~/fmri_tui-*/` folders. The Docker images stay,
as the toolbox finds them by name, so `setup_docker_image.sh` only needs running again to change
the defacer.

On OpenMediaVault, check first: your user has a home folder (Users > Settings) and SSH access,
`docker info` works without sudo, and the home folder isn't on a disk mounted `noexec`.

### Crosscheck the behaviour logs

1. If the logs are on OneDrive, make sure it is running and synced. Back up: **Save now** on the Setup
   tab.
2. See what is wrong (nothing is changed):

   ```bash
   fmri_behav_check path/to/logs path/to/dicoms --bids path/to/bids --problems-only
   fmri_behav_check path/to/logs path/to/dicoms --bids path/to/bids --log emolabeling/2024/P001-....log
   ```

3. Fix it in the window (`fmri_toolbox`) or the terminal app (`fmri_tui`): wrong IDs, dates and session
   numbers of scans first (**Raw DICOMs**: one wrong folder can leave several logs unmatched), then
   the logs (**Behaviour logs**: ID, date, **Use this one**, **Mark OK** / **Mark error**, **Mark
   crosschecked**). **Save**, then **Convert to BIDS**.
4. Run the check again.

What to look for: *unknown ID* (a typo, or a twin: `12345-1` becomes `12345T1`), *date mismatch*, *duplicate*,
*no task scan*, *no task* (the path needs `cogreappraisal` or `emolabeling`), and *aborted* logs, which
match but need a verdict. A match only says that ID and date agree: look at the events too.

### The other checks

```bash
fmri_bids_check path/to/bids                            # each session's problems
fmri_bids_check path/to/bids --lengths                  # scans too short or too long
fmri_bids_check path/to/bids --dicom path/to/dicoms     # missing volumes
fmri_mriqc path/to/bids path/to/mriqc                   # run MRIQC; metrics with the outliers marked
```

### Dummy data to try it on

```bash
fmri_generate_sample_data path/to/dummy --with-errors --seed 42
fmri_generate_sample_data path/to/dummy --reference-behav path/to/logs --reference-dicom path/to/dicoms
fmri_generate_sample_data path/to/dummy --with-errors --bids-folder path/to/dummy_bids
```

The first makes 5 clean participants and one per known problem; `dummy_data_log.txt` says what each is
for. The second finds the `.log` files in every folder below the logs folder and gives each log and
session folder a dummy twin: IDs and dates match, or don't, exactly where the real ones do. IDs, dates,
times, answers, names and images are made up; the real data is only read, the link between real and
dummy IDs is never written down, and the result is deleted if anything real is found in it. What stays
is the shape: how many logs and sessions, the order of the dates, the logs' layout, the number and kind
of their events, and which of them were answered. Look through it before sharing it. The dummy folder
must be new, and outside the real data.

## 1a. Preprocessing via C-PAC in Docker (built, not yet run on real data)

```powershell
fmri_preprocess <BIDS_ROOT> <OUTPUT> [--subject_id P001] [--task Rest [--task Matching]] [--rerun] [--parallel 1] [--n-cpus 4] [--mem-gb 8]
```

- Runs C-PAC's standard `preproc` preset (`fcpindi/c-pac:release-v1.8.7`), one container
  per subject. This answers the "Windows-native vs container" question below for now:
  FSL/AFNI/ANTs run inside the container.
- Only `sub-<id>/` and `dataset_description.json` are mounted, so `crosscheck_pending/` is
  never used as input.
- Output goes to `OUTPUT/output/pipeline_cpac_preproc/` and logs to `OUTPUT/log/`. A subject
  is skipped when every session already has a `desc-preproc_bold`, unless `--rerun` is passed.
- `--parallel N` (GUI: "Subjects ... at once") runs N subjects side by side, each in its own
  container with `--n-cpus` and `--mem-gb`, like the philani CHPC jobs. Many C-PAC steps use
  one core, so this finishes a batch sooner. When N x CPUs or N x memory is more than Docker
  allows, you get a warning and a suggestion that fits (about 10 GB and at least 2 CPUs
  per subject; the GUI's "Suggest" button fills it in). Container starts are spaced 10 s apart.
- The GUI ticks BOLD tasks in a list (all ticked = every task); each subject still runs once,
  with its ticked tasks together (`--bold_label task-A task-B`). While running, "Stop now"
  stops the containers (`docker stop`), freeing CPU and memory for other work on the PC;
  running the batch again later resumes each stopped subject from its last finished step
  (shown as ■ stopped). Closing the window offers the same. Containers are named
  `fmri_cpac_sub-<id>`, so a subject can't be started twice.
- `fmri_preprocess_gui` does the same from a window (vr_lab_toolbox process-results
  layout): pick the BIDS and output folders, tasks, subjects at once, CPUs and memory, then
  click "Preprocess". Step-by-step guide with a numbered screenshot:
  [docs/preprocessing.md](docs/preprocessing.md).
  It shows each subject's status, its preprocessed BOLD files, and the end of its C-PAC log.
  Clicking a subject also previews what C-PAC already generated for it: the QC figures
  (framewise displacement, motion, BOLD SNR; zoomable), brain slices (sagittal / coronal /
  axial grids with mask outlines: T1w + brain and tissue masks, mean BOLD + BOLD mask,
  BOLD on T1w for coregistration, T1w and BOLD in template space for normalisation;
  neurological convention, left on the left) and the per-run motion summary table.

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
  app/          # what both front ends share, without Qt: state, jobs, tab list, texts
  gui/          # the PySide6 window (display only)
  tui/          # the Textual terminal app, fmri_tui (display only)
tests/
```

## Development

- Python 3.13, `.venv` in the repo root:
  `pip install -e ".[gui]" -r requirements-dev.txt`, with the environment active
  (`pip install -e .` leaves out Qt, and with it the window)
- Style: [Golden Rules](docs/golden-rules.md) and [AI Style Guide](docs/ai-style-guide.md).
  Agent rules are in [AGENTS.md](AGENTS.md).
- Build: `.\build.ps1 -Exe | -Full | -Inno` (all output goes to `build_output/`); the bundle for
  Linux servers: `./build.sh --tui` ([docs](docs/linux-bundle.md)).
- Docs: `mkdocs serve`
