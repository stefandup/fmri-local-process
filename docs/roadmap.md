# Roadmap

Possible next steps, not yet started. Each needs a go-ahead before work begins.

## Terminal version of the toolbox (TUI)

**Goal:** use the toolbox without a display: on the HPC, on local PCs, and on the headless
data servers over SSH. A [Textual](https://textual.textualize.io/) app (`fmri_tui`) would
recreate the toolbox tabs in the terminal.

**Shape**

- A new layer without Qt (`app/`) holds the state and workflow logic that now lives in the
  Qt pages: remembered folders, busy locks, and the convert, trim and preprocess jobs. The
  PySide6 window and the TUI both use it, so the logic is written once.
- The TUI never imports PySide6, because Qt often won't load on a headless server. PySide6
  becomes an optional `[gui]` install; the base install has the CLIs and the TUI.
- Brain slices and QC figures are always saved as PNG files. They are also shown in the
  terminal where it supports images (Windows Terminal does; PuTTY doesn't).

**Steps**

1. Trial: header and the read-only Crosscheck overview tab, tried locally and over SSH.
2. Move the logic out of the Qt pages into `app/`; the Qt window keeps working throughout.
3. TUI tabs: Crosscheck and Preprocess first, then Scan lengths, Behaviour logs, Convert.
4. Packaging: optional Qt install, and the TUI inside the HPC container image.

**Open questions:** adding `textual` (and later `textual-image`) as dependencies; whether
the image approach is right; trial first, or a full plan for step 2.

## C-PAC on the HPC (Apptainer + PBS)

Run preprocessing on the HPC with Apptainer instead of Docker, one PBS job-array entry per
subject. The working folder goes in a per-subject overlay image so an interrupted run can
resume. This also gives the TUI a C-PAC runner for the HPC and the data servers.
