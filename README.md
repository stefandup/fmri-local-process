# fmri_local_process

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
4. **Packaging.** Add `fmri_behav_events` to the PyInstaller and installer builds if it
   should ship. It is only on PATH via `pip install -e .` for now.
5. **Linux build.** Run `./build.sh` and `packaging/install.sh` on Ubuntu.

## Linux: one PC or server (e.g. OpenMediaVault)

The whole toolbox without a display: the terminal app `fmri_tui` and command-line tools that do the
checking on their own. Needs a 64-bit Intel/AMD PC and, for C-PAC, MRIQC and defacing, Docker.
Details: [the Linux bundle](docs/linux-bundle.md), [the terminal app](docs/tui.md).

### Build (here) and install (there)

```bash
./build.sh --tui                                   # in WSL or Linux, with Docker: like build.ps1 -Full
packaging/linux/check_bundle.sh                    # optional: tries it in clean containers

scp build_output/installer/fmri_tui-*-linux-x86_64.tar.gz user@server:
ssh user@server
tar -xzf fmri_tui-*-linux-x86_64.tar.gz && cd fmri_tui-*/
./install.sh                                       # no sudo; then open a new terminal
docker/setup_docker_image.sh --defacer pydeface --mriqc   # C-PAC, MRIQC, defacer (~20 GB)
```

On OpenMediaVault, check first: your user has a home folder (Users > Settings) and SSH access,
`docker info` works without sudo, and the home folder isn't on a disk mounted `noexec`. Keep the
data on the data disk; the app needs about 400 MB.

### In WSL or Linux, from this repo (no bundle)

As in vr_lab_toolbox: a virtual environment and `pip install -e`. The commands are in `pyproject.toml`
(`[project.scripts]`), and an active environment puts them on the PATH.

```bash
python3.13 -m venv .venv                     # once
source .venv/bin/activate                    # in each new terminal (VS Code does it for you)
pip install -e ".[gui,tui]"                  # once, and again when pyproject.toml changes
```

`[tui]` adds the terminal app and `[gui]` the window; `pip install -e .` alone is enough for the
commands. The bundle's `install.sh` needs no environment: its commands are on the PATH everywhere.

### Set the folders once

```bash
fmri_folders --dicom DICOMS --behav LOGS --bids BIDS --output CPAC_OUT --mriqc MRIQC_OUT
```

Shared with `fmri_tui`. After this no command needs a folder. (Given folders win, and are never mixed
with the remembered ones. The Windows window keeps its own.)

### Crosscheck the behaviour logs

Back up first (`fmri_bids_fix backup`, or Save now on the Setup tab). If the logs are on OneDrive,
make sure it is running and synced.

| Step | Terminal app (or window) | Command |
|---|---|---|
| See what doesn't match | **Behaviour logs**, tick *Needs review only* | `fmri_behav_check --problems-only` |
| Wrong ID, date or session number of a scan: do these first, one folder can leave several logs unmatched | **Raw DICOMs**, type, **Use** | `fmri_behav_fix folder FOLDER --id P001 --session 2` |
| Wrong ID or date of a log | **Behaviour logs**, type, **Use** | `fmri_behav_fix log LOG --id P001 --date 2024-04-03` |
| Look at a log's events, and whether they fit the BOLD run | highlight it | `fmri_behav_check --log LOG` |
| Several logs on one day | **Use this one** | `fmri_behav_fix use LOG` |
| Verdict on a short or odd log | **Mark OK** / **Mark error** | `fmri_behav_fix log LOG --verdict error --note "why"` |
| Been through it | tick, **Mark ticked crosschecked** | `fmri_behav_fix log LOG --crosschecked` |
| Keep it | **Save** | (saved at once) |
| Apply it | **Convert to BIDS** | `fmri_convert_to_bids` |

`LOG` is any part of the log's path that only it has, e.g. `P001-emo`. Nothing changes the raw files:
decisions are saved in `BIDS/code/`, the same files for the window, the terminal app and the commands.

What to look for: *unknown ID* (a typo, or a twin: `12345-1` becomes `12345T1`), *date mismatch*, *duplicate*,
*no task scan*, *no task* (the path needs `cogreappraisal` or `emolabeling`), and *aborted* logs, which
match but need a verdict. A match only says that ID and date agree: look at the events too.

### The rest by commands

```bash
fmri_bids_check                    # each session's problems; --files --lengths --volumes --csv out.csv
fmri_bids_fix --help               # mark, note, keep, trim, expect, delete-..., backup
fmri_mriqc                         # run MRIQC, list the metrics; --results, --rate
fmri_preprocess                    # C-PAC
```

### Dummy data to try it on

```bash
fmri_generate_sample_data dummy --with-errors --seed 42      # 5 clean participants, one per problem
fmri_generate_sample_data dummy --reference                  # like the real study (fmri_folders)
fmri_generate_sample_data dummy --with-errors --bids-folder dummy_bids         # and convert it
```

The same command line as vr_lab_toolbox's `..._generate_sample_data`; `dummy_data_log.txt` in the folder
says what each participant is for. `--reference` finds the `.log` files in every folder below the logs
folder and gives each log and session folder a dummy twin: IDs and dates match, or don't, exactly
where the real ones do. IDs, dates, times, answers, names and images are made up; the real data is only
read, the link between real and dummy IDs is never written down, and the result is deleted if anything
real is found in it. What stays is the shape: how many logs and sessions, the order of the dates, the
logs' layout, the number and kind of their events, and which of them were answered. Look through it
before sharing it. The dummy folder must be new, and outside the real data.

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
  `.venv\Scripts\python -m pip install -e ".[gui,tui]" -r requirements-dev.txt`
  (the base install has no Qt; `[gui]` adds the window, `[tui]` the terminal app)
- Style: [Golden Rules](docs/golden-rules.md) and [AI Style Guide](docs/ai-style-guide.md).
  Agent rules are in [AGENTS.md](AGENTS.md).
- Build: `.\build.ps1 -Exe | -Full | -Inno` (all output goes to `build_output/`); the bundle for
  Linux servers: `./build.sh --tui` ([docs](docs/linux-bundle.md)).
- Docs: `mkdocs serve`
