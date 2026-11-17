# Toy: the app/ layer, the window and the terminal app

A temporary, tiny version of the plan in `docs/next-steps.md`. Delete this folder once
phase A is done.

The toy tool counts the lines in the `.txt` files of a folder. It has the same shape as the
toolbox: a shared folder, a "busy" lock, a background job with progress and a stop button,
and two tabs.

| Toy file | Layer | Real counterpart |
|---|---|---|
| `toy_processing.py` | 1. the work | `processing/` (stays as is) |
| `toy_app.py` | 2. shared, no Qt | the new `app/` (state, settings, jobs, tab list) |
| `toy_gui.py` | 3a. PySide6 window | `gui/` |
| `toy_tui.py` | 3b. Textual terminal app | the new `tui/` |

Step through the tests in this order (VS Code Test Explorer, or
`pytest tests/toy_app_layer -v`):

1. `test_toy_1_app.py`: the app layer alone, no UI at all
2. `test_toy_2_gui.py`: the window drives the same app layer
3. `test_toy_3_tui.py`: the terminal app drives the same app layer
4. `test_toy_4_guards.py`: the two checks that keep the window and the TUI in step

Try the terminal app yourself: `python tests/toy_app_layer/toy_tui.py` (`q` quits).
