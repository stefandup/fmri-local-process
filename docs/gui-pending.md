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
