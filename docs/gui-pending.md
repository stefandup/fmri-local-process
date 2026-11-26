# GUI changes waiting for Windows

TUI changes made on Ubuntu that the window (`gui/`) should get too. Apply them on a new branch
from PowerShell, test the window there, then remove the entry.

## Behaviour logs: a view dropdown instead of "Needs review only"

- **TUI:** `tui/behav.py`, the `#log-view` dropdown (commit "TUI behaviour logs: view dropdown").
- **Window:** in `gui/behav_logs_dialog.py`, replace the `problems_only` QCheckBox (about line 182)
  with a QComboBox of `app.behav.LOG_VIEWS`, defaulting to the first ("All logs"). In the
  row-hiding loop (about line 592) use `setRowHidden(row, not log_shown(m, view))`, with
  `log_shown` from `app.behav`. Update the docstring near line 597 that mentions
  "Needs review only".
- **Views:** All logs · Matched and needs review · Needs review only · Matched · Unmatched.

## Raw DICOMs and Behaviour logs: Refresh cache button

- **Shared:** `load_behav_data` (`app/behav.py`) now uses a per-user scan cache, so the window
  already gets the faster loads. Check on Windows that it lands in `%LOCALAPPDATA%\fmri_local_process\scan`.
- **TUI:** a "Refresh cache" button next to Reload on both tabs (`tui/behav.py`, `#refresh`),
  tooltip `REFRESH_CACHE_TIP` from `app/behav.py`; it calls `load_behav_data(..., fresh=True)`.
- **Window:** in `gui/behav_logs_dialog.py`, add the same button next to each "Reload" button
  (about lines 784 and 927), with `setToolTip(REFRESH_CACHE_TIP)`, passing `fresh=True` to the load.

## Behaviour logs / Raw DICOMs: Match first, file name last, a details box

- **TUI:** `tui/behav.py`. The logs table uses `TUI_LOG_COLUMNS`: Match, Task, ID in name, Log
  state, Logged, Corrected ID, Corrected date, Verdict, Session, Notes, Log (just the file name).
  Under the Mark / Un-mark buttons, a bordered `DetailBox` (`#details`) shows the highlighted row:
  on tab 4 Match, Log state, Log file date, Corrected ID, Corrected date, Verdict, Notes, Events
  (`log_details`); on tab 2 Scanned, ID in folder, Corrected ID, Corrected date, Set session,
  Resolves to, Notes. The inputs and buttons below it are unchanged.
- **Window:** reorder `COLUMNS` in `app/behav.py` the same way (and the index constants
  `CORRECTED_ID` ... `NOTES`, plus `log_cells`), then drop `TUI_LOG_COLUMNS`. In
  `gui/behav_logs_dialog.py` put the same details in a QGroupBox under the Mark buttons on both
  panels.
