# fMRI Local Process

A locally installable fMRI toolbox. The steps so far:

1. [BIDS conversion](bids-conversion.md): Philani DICOMs → BIDS (`fmri_convert_to_bids`,
   plus `fmri_behav_events` for behavioural logs), then crosscheck (`fmri_crosscheck`).
2. [Preprocessing](preprocessing.md): C-PAC in Docker, from a window
   (`fmri_preprocess_gui`) or the command line (`fmri_preprocess`), with previews of each
   subject's QC figures, brain slices and motion.

QC reports and the region timeseries viewer are still at the planning stage; see the README.