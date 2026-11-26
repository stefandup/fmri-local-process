# Checkerboard task protocol

The visual checkerboard run (BIDS `task-Checkerboard`) is presented by
[checkerboard_fmri_task](https://github.com/stefandup/checkerboard_fmri_task) with its
default `task_config.toml`. This page gives the protocol exactly as those defaults set it,
how the scanner trigger starts it, and how the run is trimmed afterwards. If a session ran
with a changed config, the copy saved next to its log (`..._config.toml`) is the record of
what was actually shown.

## Design at a glance

| | |
|---|---|
| Design | Block design, alternating OFF / ON |
| First block | **OFF** (fixation only) |
| ON block | **20 s** contrast-reversing checkerboard |
| OFF block | **20 s** fixation on mean grey |
| Cycle | 40 s (0.025 Hz) |
| Task length | **200 s** from the start trigger: 5 OFF + 5 ON blocks |
| Fixation | Red dot, 0.3° diameter, shown throughout |

## Block timing

Times are in seconds from the start trigger (the first saved volume).

| Block | Condition | Onset (s) | End (s) |
|---|---|---|---|
| 1 | OFF | 0 | 20 |
| 2 | ON | 20 | 40 |
| 3 | OFF | 40 | 60 |
| 4 | ON | 60 | 80 |
| 5 | OFF | 80 | 100 |
| 6 | ON | 100 | 120 |
| 7 | OFF | 120 | 140 |
| 8 | ON | 140 | 160 |
| 9 | OFF | 160 | 180 |
| 10 | ON | 180 | 200 |

The task ends at 200 s, right at the end of the last ON block.

## Stimulus

| Setting | Value |
|---|---|
| Pattern | Radial (polar) checkerboard: 24 wedges × 10 rings, ring widths scaled with eccentricity (log spacing) |
| Size | 0.5° blank centre to 8.0° outer radius |
| Contrast reversal | 7.5 reversals/s (3.75 Hz full flicker cycle; 8 frames per reversal at 60 Hz) |
| Contrast | 100 % Michelson: black and white checks around a mean grey of 0.5 |
| Background | Mean grey (0.5) in ON and OFF blocks, so average screen luminance does not change between conditions |
| Display assumed | 60 Hz projector, image 53.0 cm wide, viewed from 60.0 cm (via the mirror) |

## Scanner trigger

- The scanner sends a **Left Shift** key press as its trigger, one per volume.
- The scanner acquires its **dummy scans before it sends its first trigger**, and does not
  save them. So `dummy_triggers = 0`: the **first trigger starts the task**, and the task's
  time 0 is the first volume in the NIfTI.
- Every trigger is written to the event log, so the log can be checked against the volume
  count.
- If a scanner instead triggers on its dummy scans too, set `dummy_triggers` to that number;
  the task then starts on trigger `dummy_triggers + 1`. This was not the case for this study.

## Running a session

1. Start **Checkerboard fMRI Task**, type the participant ID, and pick the projector screen
   if asked.
2. The task measures the refresh rate (about 2 s), then shows the fixation dot and waits.
3. Start the scanner. The first trigger starts block 1 (OFF).
4. After 200 s the task ends by itself. **Escape** stops it early; the log is saved either
   way.

The data folder gets `sub-<ID>_task-checkerboard_<date-time>_events.csv` (every trigger,
block start, reversal, dropped frame and timing warning, with `task_time_s` counted from the
start trigger) and a copy of the config used.

## Expected length and trimming

Because the task starts on the first saved volume, a run should hold exactly the task:

**expected volumes = 200 s ÷ TR, rounded up** (e.g. 241 volumes at TR 0.832 s).

The scanner is often stopped by hand a little after the task ends, so a run can have extra
volumes at the end. In the toolbox (and the review package), tab **Scan lengths &
trimming** uses this task-design length for the checkerboard (shown as "task design (N)")
rather than the median of the runs, so runs that were all stopped late are still caught.

- **Nothing is trimmed from the start.** There are no dummy volumes in the NIfTI to remove.
- **Long runs are trimmed at the end**: **Trim to N...** keeps the first N volumes and
  deletes the rest from the BIDS file. The original count is recorded in the sidecar
  (`TrimmedFromVolumes`), and the raw DICOMs keep every volume.
- Runs **shorter** than the expected length are flagged at the BIDS Crosscheck; they did
  not record the whole task.
- Trimming is done here, before preprocessing, never during it.

## Analysis model

First-level stats model the same design: the ON blocks above as a boxcar starting at the
first volume, convolved with SPM's canonical HRF. A 0.01–0.1 Hz band-pass keeps the 40 s
block cycle. See [First-level stats](first-level-stats.md).
