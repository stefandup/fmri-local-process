# Toy: the app/ layer, the window and the terminal app

A temporary, tiny version of the plan in `docs/next-steps.md`. Delete this folder once
phase A is done.

The toy tool counts the lines in the `.txt` files of a folder. It has the same shape as the
toolbox: a shared folder, a "busy" lock, a background job with progress and a stop button,
and two tabs.

| Toy file | Layer | Real counterpart |
|---|---|---|
| `toy_gui_0_plain.py` | all in one: today's way | `gui/` as it is now |
| `toy_processing.py` | 1. the work | `processing/` (stays as is) |
| `toy_app.py` | 2. shared, no Qt | the new `app/` (state, settings, jobs, tab list) |
| `toy_gui.py` | 3a. PySide6 window | `gui/` |
| `toy_tui.py` | 3b. Textual terminal app | the new `tui/` |

`toy_data.py` makes the synthetic `notes` folder the tests and demos count.

Start by running the plain window, then the split one. They look and behave the same, and
each pauses 1 s per file so you can watch the thread fill the table:

    python tests/toy_app_layer/toy_gui_0_plain.py
    python tests/toy_app_layer/toy_gui.py

Then step through the tests in this order (VS Code Test Explorer, or
`pytest tests/toy_app_layer -v`):

0. `test_toy_0_plain_gui.py`: the plain window, and how a Qt test reads
1. `test_toy_1_app.py`: the app layer alone, no UI at all
2. `test_toy_2_gui.py`: the window drives the same app layer
3. `test_toy_3_tui.py`: the terminal app drives the same app layer
4. `test_toy_4_guards.py`: the two checks that keep the window and the TUI in step

## Debug harnesses (`debug_toy_*.py`)

One per step, for stepping through in the VS Code debugger (Run and Debug -> "Debug toy
step N"). Each calls the toy's real code with fixed inputs, keeps its files in
`<temp>\toy_debug\stepN`, and prints what it saw at the end. They aren't tests (pytest skips
them) and nothing in the toy was changed for them. To remove them: delete
`debug_toy_*.py` and the five "Debug toy step" entries in `.vscode/launch.json`.

Try the terminal app yourself: `python tests/toy_app_layer/toy_tui.py` (`q` quits).
