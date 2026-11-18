# The toolbox window

Open it from a terminal (with `.venv` active):

```powershell
fmri_toolbox
```

A splash screen shows while it loads (a few seconds). To use your own picture, save it as
`src/fmri_local_process/gui/assets/splash.png` (about 640 x 360); until then a plain one is
drawn. Loading messages appear at its bottom left.

One window, with a tab per step, in the order you do the work. Everything in BIDS is put
right (IDs, logs, scan lengths, runs, dud sessions) before it is preprocessed:

| Tab | What you do there |
|---|---|
| **1. Setup & Convert to BIDS** | Choose the raw folders (DICOM sessions, behavioural logs) and the BIDS folder they are converted into (raw ➜ BIDS). Set **Sessions per subject** and **Deface T1w** (greyed out, with a warning, when it can't be used: no defacer installed, or Docker not installed or not running. **Start Docker** starts Docker Desktop and checks again; **Check again** looks once more), **Scan rules...** sets which series folders become which BIDS scan. Then **Convert to BIDS**, with a progress bar and a summary of what happened. The **Crosscheck backup** box saves and restores the crosscheck (below). **Help ?** explains the tab. |
| **2. Raw DICOMs** | Every DICOM session folder with its scan date/time and ID: correct a folder's ID or scan date, or set its session number (e.g. 2 when the first visit is missing). |
| **3. Sequence overview** | A subject's runs of one scan side by side with their scan parameters (NIfTI header, BIDS sidecar, raw DICOM), differences marked. See [Sequence overview](sequence-overview.md). |
| **4. Behaviour logs** | Match each log to a scan session; mark logs OK, error, or the one to use. **Export logs...** saves every log as an events.tsv in a folder you choose, all in one folder or in BIDS sub-/ses-/func folders. The logs are read in the background as soon as the toolbox opens (progress bar), so the tab is ready; **Re-check** shows a progress bar too. |
| **5. Scan lengths & trimming** | Set how many volumes each BOLD scan should have (saved as you type), and trim runs that ran on too long with the **Trim** button at the top left. |
| **6. BIDS Crosscheck** | Go through the sessions: keep one of several runs, delete files or dud sessions, add notes, and **Mark crosschecked** once reviewed (☑; tick several, or **Select all**, to mark them at once). The BIDS Folder panel at the top shows which folder this is, with **Rescan** and **Reveal**. |
| **7. Scan quality (MRIQC)** | Run MRIQC on the BIDS folder and read its image quality metrics per run, outliers highlighted. Select a run to see the scan below (drawn from the BIDS file: mean image, tSNR, signal and DVARS over time, carpet plot; slices for a T1w) and rate it **1 bad: discard**, **2 unsure** or **3 keep**, with an optional comment. Ratings are saved at once in `toolbox_qc_ratings.tsv` in the MRIQC output folder. |
| **8. Preprocess BIDS (C-PAC)** | Run C-PAC on the crosschecked BIDS folder, with previews of each subject's results; its output folder is chosen here. See [Preprocessing](preprocessing.md). |
| **9. Crosscheck overview** | The whole BIDS folder at a glance, like the old Streamlit BIDS viewer: summary counts shown as a log (typewriter font), then one table to scroll through (a row per session, runs and volumes per scan) with the cells to look at coloured: red missing, yellow short, orange several runs or not defaced, blue too long. **Problems** says in a few words what is wrong, e.g. `trim: task-Rest_bold; missing: task-Matching_events`. Double-click a **Note** to write one there. **Export CSV...** saves that table. The **Action log** tab lists every change anyone made, newest first, with a search box. |
| **10. First-level stats** | Fit a task GLM to each preprocessed run of a subject and compare the runs (e.g. ses-1 and ses-2 sequences). The first subject is modelled while the splash screen shows, so the tab opens on results. See [First-level stats](first-level-stats.md). |

Each tab starts with one bold line saying what it is for.

The details of each step are in [BIDS conversion](bids-conversion.md) and
[Preprocessing](preprocessing.md), [First-level stats](first-level-stats.md), [Sequence overview](sequence-overview.md).

- **Look.** The window uses Qt's Fusion style, as the vr_lab_toolbox tools do: crisper
  contrast, and the colours set here render the same everywhere. Explanations live in
  tooltips (hover a panel or button) and behind each tab's **Help ?** button.
- **Folders are shared.** The folders chosen on tab 1 are the ones every tab works on; the
  BIDS folder's name is shown above the tabs and on the BIDS Crosscheck tab. The Preprocess
  tab's own folder panels follow (and the other way round). The first time, the toolbox
  takes over the folders the separate crosscheck and preprocess windows remembered.
- **One job at a time changes the BIDS folder.** While a conversion, a trim or C-PAC is
  running (shown top right), the other tabs' buttons that change files are greyed out:
  Convert, Delete, Keep run, Trim and Preprocess. Notes, looking around and the logs tab stay
  open.
- **Tabs refresh when you open them** if the BIDS folder changed since. Tab names show what
  is waiting, e.g. **6. BIDS Crosscheck (6 to check)** or **4. Behaviour logs (3 to
  review)**.
- **No console windows.** On Windows, the programs the toolbox starts (docker, dcm2niix)
  run without a window of their own, so nothing flashes up while Docker is checked.
- **Closing** while C-PAC runs asks whether to stop it or leave it running. Closing during a
  conversion or trim stops it; the scan it was on is left out of the BIDS folder and done
  again next time.

`fmri_crosscheck` and `fmri_preprocess_gui` still work: they open this window on their tab.

## Saving and restoring the crosscheck

Everything decided in the toolbox (ID, date and session corrections, log verdicts, deleted
sessions, notes, crosscheck marks, settings, expected lengths, kept runs, trims, and the
action log) lives in the BIDS folder's `code/`. The **Crosscheck backup** box on tab 1 copies
those files outside the BIDS folder, into your app-data folder under the **Study ID**, so they
survive the BIDS folder being deleted, corrupted or moved:

- **Auto-save every N min** (on by default, every 2 minutes) and **Save now**. Each save
  updates `latest` and keeps one dated copy per day. A save never drops a file from a
  backup, so a BIDS folder that lost its `code/` can't wipe the last good copy.
- **Restore...** picks `latest` or a day's copy, puts its files back into the BIDS folder,
  converts again from raw (which applies the corrections and deleted sessions), then replays
  the kept runs and trims. Anything it can't replay is listed in the Activity Log. It is for
  an empty or freshly made BIDS folder, not to merge into one with its own changes.
- **Reveal** opens the backup folder.

The approach follows vr_lab_toolbox's Save / Restore Crosscheck Data.