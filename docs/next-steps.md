# Next steps: headless server and HPC

The plan for the branch `headless-tui`, and the choices behind it. It expands the
[Roadmap](roadmap.md) items *Terminal version of the toolbox* and *C-PAC on the HPC*.
Phases A to C are built; what was done and what
is still to check (Windows, the real server) is in the [run log](headless-tui-log.md).

## Why

The toolbox window (`fmri_toolbox`) needs a display. We also want the full toolbox:

- on a **headless data server**, used over SSH, through a terminal app (TUI);
- on the **HPC**, with Apptainer instead of Docker and PBS jobs for the heavy steps.

The TUI should rest on the existing code and look and work like the toolbox window as far
as a terminal allows. When the window gets a new feature later, the TUI needs the same
feature, so that should be easy to do and hard to forget.

## Where the code stands

- `processing/` (the actual work) has no Qt in it, so a TUI can use it as is.
- A lot of the workflow lives inside the Qt pages, though: the jobs that run in the
  background (`PreprocessThread`'s batch loop, `MriqcThread`, `ConvertThread`, `TrimThread`,
  `BehavLoadThread`, ...), the shared state (`ToolboxState`, built on Qt's `QObject` and
  `QSettings`) and many small text helpers. A TUI can't import any of these, because importing
  them loads Qt.
- `gui/` is about 9,800 lines over 25 files; `preprocess_gui.py` (1,800) and
  `behav_logs_dialog.py` (1,300) are the biggest.

## Running it (agreed 25 November 2026)

`master` is merged into this branch. The work continues in WSL Ubuntu, with as much as possible
done in one overnight run and tweaks the next day.

1. **Set up WSL** (Decision 4): clone onto WSL's own disk, `uv` for Python (Ubuntu 26.04 has
   only Python 3.14 and no `ensurepip`; the Windows `.venv` is on 3.13), and copy
   `safe_data_source/` over, as it isn't in git. Two environments: one with PySide6, to
   run the window's tests without a screen (`QT_QPA_PLATFORM=offscreen`), and one without
   Qt, for everything else. Run the tests before changing anything, as the baseline.
2. **Phase A**, then **B**, then **C** (Decision 5), in that order. Phase A step 5 starts from
   `processing/tab_guide.py`, which already holds the tab list without Qt.
3. **One commit per phase**, at least, once its checks pass, so each phase can be rolled
   back. Say "Phase A/B/C" in the commit message.
4. **If something blocks a phase**, write down what and why, and go on only with work that
   doesn't depend on it. Never commit a phase as done with failing checks.
5. **The next day, on Windows:** the window on `safe_data_source/` and `build.ps1 -Full`. The
   Windows build and registry settings can't be checked in WSL, so nothing is merged into
   `master` before this.

**Keep the toy.** `tests/toy_app_layer/`, its "Debug toy step" entries in
`.vscode/launch.json` (not in git, so only in the Windows checkout) and
[its page](app-layer-toy.md) stay as they are: don't remove, move or rewrite them, also not once
the real `app/` layer exists. The user still has to review the toy in depth. The plan as a whole
looks good to the user.

**Use bash in WSL.** The PowerShell rules (pwsh commands in `AGENTS.md`, the assistant's own
"use PowerShell" note) are for the Windows checkout only.

**Decided:** the toolbox itself is installed directly on the host (Decision 3), not in a container.

**Known before we start:** on Windows the whole test suite passes, but `pytest` then crashes as
it exits (code -1073741819, an access violation). It did so before the merge too, and the
cause isn't known. Read the pass/fail line, not only the exit code.

## Decision 1: how the code is organised

**Chosen: a new Qt-free `app/` layer, and the window and the TUI both sit on it.**

```
src/fmri_local_process/
  processing/   the work itself (unchanged)
  app/          NEW, no Qt: shared state, settings, tab list, background jobs, text helpers
  gui/          the PySide6 window: display only
  tui/          NEW, the Textual terminal app: display only
  cli/          command-line tools
```

| Option | Pros | Cons |
|---|---|---|
| **`app/` layer (chosen)** | Logic written once; a change is app → window → TUI; the jobs can be tested without any UI; the window gets smaller | A refactor of working code first; the window must keep working throughout |
| TUI copies the logic from the Qt pages | Fastest start; the window isn't touched | Two copies that drift apart; every fix twice |
| TUI only calls the CLIs | No refactor | The CLIs don't cover everything the tabs do (crosscheck marks, trimming, review); no shared state or live progress |

[The app/ layer, shown on a toy](app-layer-toy.md) walks through the whole idea on a
small working example.

Steps, with the window working and all tests passing after each one:

1. **Split the dependencies.** PySide6 becomes an optional `[gui]` install, and Textual a
   `[tui]` install. The base install has no Qt. A test checks that importing `processing`,
   `app` and `tui` never loads PySide6.
2. **Events without Qt.** A small `Event` class with `.connect()` and `.emit()`, the same calls
   as Qt signals, so the window's code barely changes.
3. **Settings.** The window keeps `QSettings` (on Windows these are in the registry, so remembered
   folders survive). The TUI stores JSON in `~/.config/fmri_local_process/`, or in a review
   package's own settings folder.
4. **Shared state.** `ToolboxState` moves to `app/state.py` as a plain class.
5. **Tab list.** The tab names and order, and which tabs a view-only package hides, move to
   `app/tabs.py`.
6. **Background jobs.** Each job's loop becomes a plain class in `app/` that reports through
   callbacks and can be stopped. The window wraps it in a `QThread`; the TUI wraps it in a
   Textual worker. Order: trimming, converting, reading behavioural logs, C-PAC batch, MRIQC
   batch, then the smaller readers (sequence, first-level, DICOM volumes, image download).
7. **Text helpers** (report lines, problem text, summaries, notices) move to `app/`.
8. **Figures as PNG files** (`app/figures.py`): the window and the TUI show the same images.
9. **Parity test.** Every tab in the tab list must have a TUI page or be on an explicit
   "not yet in the TUI" list. A tab added only to the window fails the tests.

After step 9, the Windows build (`build.ps1 -Full`) still builds and the window works as
before. This part can be merged on its own.

## Decision 2: the terminal app

**Chosen: [Textual](https://textual.textualize.io/)**, with `textual-image` for pictures.

| Toolbox window | TUI |
|---|---|
| Tabs with numbers and badges | Tabs, from the same tab list |
| Folder dialog | Path box + folder tree |
| Yes/No questions | Confirm screen |
| Tables | `DataTable` |
| Log panes | Scrolling log |
| Progress bars | Progress bars |
| Tooltips | Help line / `?` key |
| Images | PNG file path, shown inline where the terminal supports it |
| MRIQC HTML reports | File path only |

**Images:** always written as PNG files, and shown inline when the terminal can (Windows
Terminal, WezTerm, kitty). Over a plain SSH terminal such as PuTTY you get the file path.

| Option | Pros | Cons |
|---|---|---|
| **PNG + inline where possible (chosen)** | Works everywhere; looks good in a modern terminal | One more dependency |
| PNG files only | Simplest | You have to copy files off the server to look at them |

Tab order: (1) header, Setup folders, Crosscheck overview; (2) Crosscheck, Scan sessions,
Behaviour logs, Scan lengths & trimming; (3) Setup convert, MRIQC, Preprocess; (4)
First-level stats, Sequence overview.

## Decision 3: builds and packaging

**One build per target? No: two builds, plus a runtime choice.**

| Target | Build | C-PAC, MRIQC, defacer run in |
|---|---|---|
| Windows desktop | the current `build.ps1 -Full` (with Qt) | Docker Desktop |
| Headless server | **one Linux bundle** (TUI + CLIs, no Qt) | Docker or Apptainer |
| HPC | **the same Linux bundle** | Apptainer, via PBS jobs |

The server and the HPC differ only in which container program runs the heavy tools. The
toolbox picks it at run time, from a setting or from what is installed. That doesn't need
a separate build.

**How the toolbox itself gets onto the server.** The tools that are hard to install (C-PAC,
MRIQC, FreeSurfer's `mri_deface`) are already Docker images. The question is how to ship the
toolbox, which only needs Python packages and dcm2niix.

| Option | Pros | Cons |
|---|---|---|
| **PyInstaller bundle, `.tar.gz` + `install.sh` (chosen)** | No Python needed on the server; same approach as the Windows build and the current `build.sh`; runs Docker/Apptainer/`qsub` directly on the host | Must be built on an old Linux (glibc) to run on every server: built in a `rockylinux:8` container, so it works on Ubuntu 22.04+ and RHEL/Rocky 8 |
| Toolbox in its own container image | One `docker run` / `apptainer run`; consistent environment | It has to start *other* containers from inside a container: Docker needs the host's socket mounted (full root access) with identical paths; Apptainer inside Apptainer is fragile; `qsub` isn't available inside the container |
| Toolbox inside the C-PAC image | C-PAC runs without starting a second container | MRIQC is a separate image with its own Python, so the nesting problem comes back; a ~15 GB image for every toolbox update |
| pip / pipx wheel | Simplest to build | Needs Python 3.11+ on the server, and dcm2niix installed separately |

A small toolbox Docker image for servers that only have Docker can come later, as an extra.

## Decision 4: develop in WSL

**Yes.** WSL2 Ubuntu is a real Ubuntu, so it is a good test for an Ubuntu server, with these
differences to work around:

| WSL differs from a server in | So |
|---|---|
| WSLg gives it a display, so Qt *would* load | Test with `DISPLAY` and `WAYLAND_DISPLAY` unset and PySide6 not installed |
| `/mnt/c` is slow and mangles file permissions and line endings | Clone into WSL's own disk (`~/src/fmri_local_process`), with the Windows repo as its remote; open it with VS Code's WSL mode; a separate Linux `.venv` |
| Docker comes from Docker Desktop, not a native Docker service | Fine for development; the last test runs on the real server |
| Windows Terminal shows inline images; PuTTY doesn't | Also test the image fallback over a plain SSH terminal |
| No PBS | PBS submission is tested on the HPC itself (next branch) |

Last check that it is really headless: install the bundle in clean `ubuntu:22.04` and
`rockylinux:8` containers, and run it once over `ssh localhost` inside `tmux`.

The Windows checkout stays as it is for the toolbox window and the Windows build.

## Decision 5: branch scope

**Chosen: one branch, in three phases**, with phase A mergeable on its own:

- **A.** Move the logic out of the window into the `app/` layer (Decision 1).
- **B.** The TUI, tab by tab (Decision 2).
- **C.** The Linux bundle: `build.sh --tui` in a `rockylinux:8` build container, without Qt,
  installing `fmri_tui` and the CLIs (Decision 3).

| Option | Pros | Cons |
|---|---|---|
| **Phases on one branch, A merged first (chosen)** | The window gets cleaner right away; later GUI work already builds on `app/` | Two merges |
| Everything in one go | One merge | The branch drifts from `master` while the window keeps changing |
| Only phase A | Lowest risk | Nothing new to use yet |

## Next: when the connection drops

**Now:** jobs belong to the program that started them (`fmri_tui`, or `fmri_convert_to_bids`,
`fmri_preprocess` and `fmri_mriqc`), and it doesn't reconnect to them. In `tmux` it keeps running when
the connection drops (`tmux attach` returns to it). Without `tmux` it ends with the terminal, and:

| Job | When the terminal goes | When started again |
|---|---|---|
| Convert to BIDS | Stops: it runs inside the program | Finished scans are kept; the one it was on is redone |
| Defacing | Stops with the conversion; its container may finish on its own | Done again by Convert |
| C-PAC | Running containers should carry on, unwatched; the rest of the batch doesn't start | Once they are done, run the batch again: unfinished subjects reuse their finished steps |
| MRIQC | The running container should carry on; no further subjects start | Finished subjects are skipped |

- A C-PAC subject rerun while its container is still running is reported as failed (the container's
  name is taken). `docker ps` shows what is running.
- The program writes the jobs' console logs, so these stop at the disconnect.
- A shutdown or reboot ends everything, `tmux` included.

Tested on 25 November 2026 in WSL, with stand-in containers and by closing the terminal: in `tmux`, the
app and the containers kept running; without a terminal, the app ended and the containers stayed.
**Not tested:** real C-PAC, MRIQC or defacer containers, the command-line tools, and the real server.
The "when started again" column is from the code.

| Option | Pros | Cons |
|---|---|---|
| **Run it in `tmux` (as now; recommended)** | Nothing to build; covers every job | Has to be remembered; may need `sudo apt install tmux` |
| **Warn when `fmri_tui` starts over SSH without `tmux`** (small; worth adding) | Catches the forgotten `tmux` | Only a warning; `screen` has to count too |
| Reconnect to containers still running | C-PAC and MRIQC are watched again after a disconnect | More code; the batch's queue has to be saved and continued; doesn't help Convert; `tmux` already covers it |

To do:

1. Try a disconnect on the real server with C-PAC and MRIQC, with and without `tmux`.
2. Decide on the warning. Build the reconnecting only if `tmux` turns out not to be enough.

## Not in this branch: HPC runner

From the [Roadmap](roadmap.md): a container runner (Docker or Apptainer: mounts, stopping,
CPU use from `/proc`), PBS job arrays (one subject per entry), and `.sif` images for C-PAC,
MRIQC and the defacer. The per-subject working folder goes in an overlay image so a run can
resume.

## Working rule once this is in

Logic goes in `processing/` or `app/`; `gui/` and `tui/` only display it. A change to the
window comes with the matching TUI change, or with an entry on the "not yet in the TUI" list,
which the parity test enforces. This rule goes into `AGENTS.md`.

## How we check it

- After every step: `pytest`, `ruff check`, `ruff format`, `pyright`, and a quick run of the
  window on `safe_data_source/`.
- The no-Qt import test and the tab parity test run with the other tests.
- The jobs in `app/` get tests with stand-in callbacks and synthetic data (no Docker).
- TUI tests use Textual's built-in test driver (`App.run_test()`), run in WSL without a display.
- Phase C: install and start the bundle in clean `ubuntu:22.04` and `rockylinux:8` containers.
