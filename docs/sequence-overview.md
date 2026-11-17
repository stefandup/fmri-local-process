# Sequence overview

Tab **8. Sequence overview** lists a subject's runs of one scan side by side, with their scan
parameters. Use it to see exactly how two sequences differ, for example the checkerboard in
ses-1 and ses-2. It shows the list as a plain-text table, like the toolbox's logs:

```text
  Parameter                 ses-1          ses-2
------------------------------------------------------
  [Image (NIfTI)]
  Matrix (voxels)           64 x 64 x 48ʰ  64 x 64 x 48ʰ
≠ Volumes                   251ʰ           265ʰ
  [Timing]
≠ TR (s)                    0.832          0.8
? Flip angle (deg)          52             -
```

- **≠** marks rows where the runs differ.
- **?** marks rows found for some runs only.
- Series number, acquisition time and SAR always differ between runs, so they are shown but
  never marked.

Pick the **Subject** and the **Scan**. Tick **Only rows that differ** to shorten the list.
**Save...** writes the table as text (as shown) or as CSV.

## Where the values come from

| Source | Mark | What it gives |
|---|---|---|
| NIfTI header | ʰ | Matrix, voxel size, number of volumes, TR as stored, duration, orientation, data type |
| BIDS sidecar (`.json`) | none | What dcm2niix read from the DICOMs at conversion: scanner, protocol, TR, TE, flip angle, slice timing, multiband and in-plane acceleration, phase encoding, echo spacing, readout time, bandwidth, ... |
| Raw DICOM | ᵈ | On request, see below |

A NIfTI header holds only the image geometry and TR. The sequence parameters live in the
sidecar that dcm2niix wrote from the DICOMs. **Slice timing** is summarised from the
sidecar's `SliceTiming`: the number of slices, how many shots (and so the multiband factor),
and the slice order.

## Reading the raw DICOMs

**Read DICOM headers** finds each run's DICOM series in the DICOM folder chosen on the first
tab. It matches them as the converter did, using the session folders, the scan rules and the
ID, date and session corrections made at crosscheck. It reads the header of one file per
series and uses it:

- to **fill gaps**: a parameter missing from the header and the sidecar (for example a
  sidecar written by an older dcm2niix, or lost) is taken from the DICOM. Where the sidecar
  has a value, the sidecar wins.
- to **add DICOM-only fields** that dcm2niix doesn't copy into the sidecar: acquisition matrix,
  rows x columns, pixel spacing, phase field of view and sampling, phase encoding steps, echo
  train length, averages, imaging frequency and patient position (head first, supine, ...).

Only those named fields are read: never the patient's name, ID or birth date.

If a scan had several series, and one was picked at crosscheck, the sidecar's series number
tells which series the run came from. If it can't be told, the run gets no DICOM values, and
the summary says why: the tab doesn't guess.

Multiband and in-plane acceleration on Siemens scanners are stored in Siemens' private header,
which dcm2niix decodes. They come from the sidecar only.
