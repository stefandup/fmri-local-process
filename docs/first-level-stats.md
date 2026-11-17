# First-level stats

Tab **7. First-level stats** fits a task model to every preprocessed run of one subject and
shows the runs side by side. Use it to check that a task shows the response it should, and to
compare sessions scanned with different sequences.

## Setups

A setup is one task and its design. There is one so far:

- **Checkerboard: ON/OFF visual blocks** (task-Checkerboard, from
  [checkerboard_fmri_task](https://github.com/stefandup/checkerboard_fmri_task)). The
  defaults are that task's: OFF first, 20 s ON / 20 s OFF, 200 s, and the task starts on
  volume 5 (the 5 dummy triggers are recorded as volumes). If a session ran with another
  `task_config.toml`, change the values in the **Design** box before you click **Run**.

## What is modelled

The tab uses C-PAC's preprocessed BOLD in MNI space
(`space-MNI152NLin6ASym_reg-<n>_desc-preproc_bold`). Pick the version under **Data**:

- **reg-2** (default): motion, CSF and aCompCor regressed out, band-passed 0.01-0.1 Hz.
- **reg-1**: the same plus global signal regression. A strong visual response spreads over
  much of the brain, so regressing out the global signal can remove some of it.

The 40 s ON/OFF cycle (0.025 Hz) passes the band-pass filter. The ON blocks are convolved
with SPM's canonical haemodynamic response, and fitted per voxel with a constant and a linear
and quadratic drift (ordinary least squares).

!!! note "Read t values as relative"
    C-PAC filtered the data, so there are fewer independent time points than volumes, and t
    values come out higher than they should. Compare t between runs processed the same way.
    Don't compare them with textbook thresholds.

## Regions

Regions come from FSL's standard atlases, which ship inside the C-PAC Docker image and are in
the same MNI space as the BOLD. The first time, click **Get atlases from the C-PAC image**
(Docker must be running). It copies them once into
`%LOCALAPPDATA%\fmri_local_process\atlases`.

| Atlas | Regions |
|---|---|
| Harvard-Oxford cortical | 48 cortical regions, split into left (L) and right (R), like AAL |
| Harvard-Oxford subcortical | Thalamus, caudate, putamen, hippocampus, ... |
| Juelich histological | Cytoarchitectonic areas, including visual cortex V1-V5 (L/R) |
| MNI structural | The lobes, split into L/R |

Tick regions on and off, or filter the list by name. **Focus** ticks the setup's own regions:
for the checkerboard, the occipital lobe (occipital pole, lateral occipital, occipital
fusiform, intra- and supracalcarine, cuneal and lingual), V1-V5 in Juelich, or the occipital
lobe in MNI. With **No atlas**, the whole brain is the region.

## What you see

- **Brain views**: for each run, sagittal, coronal and axial slices through the cursor. They
  show the t map above the threshold (**t >**) on the MNI template, with the ticked regions
  outlined in cyan. Click a view to move the cursor. **Cursor to peak** jumps to the first
  run's peak.
- **Time courses**: the signal with the model fit, with the ON blocks shaded. Show it for
  each run's own peak voxel, the voxel at the cursor, or the mean of the ticked regions.
- **Average ON/OFF cycle**: the response averaged over the cycles after removing drift, with
  all runs overlaid. It is in % signal change if the data still have their mean; otherwise
  it is in residual-SD units, so runs on different scales can still be compared.
- **Comparison table**. Hover a metric's name for a short explanation. **Metric formulas...**
  opens every metric's equation, what it means, and its published references, which open in
  your browser.

| Metric | What it tells you |
|---|---|
| Peak t, beta, CNR, partial R², MNI position | The best-fitting voxel (highest t) in the ticked regions |
| Peak t per √min | Detection power per minute of scanning: fair between runs of different lengths |
| CNR (beta / residual SD) | Contrast to noise; it doesn't depend on run length. The main number for comparing sequences |
| Mean t / CNR / % change (region) | Averages over every voxel in the region |
| Region-mean t / CNR | The region's mean time series, modelled as one signal |
| Active voxels | Voxels above the threshold, and how much of the region they cover |
| Peak distance, Dice, t-map correlation | How closely each run's activation matches the first run's: location, overlap, pattern |
| Scan parameters | From the BIDS sidecar: TR, TE, flip angle, multiband, in-plane acceleration, readout, ... Rows that differ between runs are highlighted |
| C-PAC mean BOLD SNR, MeanFD, MeanDVARS | C-PAC's own QC: signal-to-noise and head motion |

**Export CSV...** saves the table. **Save maps (NIfTI)...** writes each run's beta, t and CNR
maps to `OUTPUT/first_level/sub-<id>/<setup>/`, so you can open them in other viewers.

## Comparing two sequences

- Compare **CNR** and **t per √min** first, not raw t: a longer run gets a higher t without
  being a better sequence.
- Check that head motion (MeanFD) is similar. A run with more motion has more noise left over
  after regression, whichever sequence it used.
- The temporal SNR from C-PAC (mean BOLD SNR) and the highlighted scan parameter rows show
  why two runs might differ.
- When Dice and t-map correlation are high, both sequences found the same voxels. A peak
  that moves by more than about one voxel (3 mm) is worth a look.
