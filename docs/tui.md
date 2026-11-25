# The toolbox in a terminal

`fmri_tui` is the toolbox for a computer without a display, such as a data server you reach over
SSH. It has the same tabs as [the window](toolbox.md), in the same order, and works on the same
BIDS folder, corrections and backups.

```bash
ssh myserver
tmux                # so the app keeps running if the connection drops (tmux attach to return)
fmri_tui
```

## Keys

| Key | Does |
|---|---|
| `1` ... `9`, `0` | Open that tab (`0` is tab 10); not while typing in a box |
| `[` `]` | Previous / next tab |
| `Tab`, `Shift+Tab` | Move between boxes, buttons and tables |
| `Enter` | Press a button; keep what you typed in a box |
| `Space` | Tick the highlighted row (sessions, logs, subjects, tasks, regions) |
| `y` / `n`, `Escape` | Answer a question; close it |
| `?` | Help for this tab |
| `Ctrl+Q` | Quit (asks first while something runs) |

The mouse works too, in most terminals.

## What differs from the window

- **Corrections** typed on Raw DICOMs and Behaviour logs stay with their row until you press **Use**,
  a verdict button or **Save**, also when you look at other rows in between. Only **Save** writes them;
  quitting before that asks. Notes, comments and expected lengths are saved when you leave the box.
- **Folders:** type the path and press Enter, or **Browse**. A BIDS, C-PAC or MRIQC output folder
  that doesn't exist yet is made, after asking.
- **Pictures** are PNG files. A terminal that can show pictures (Windows Terminal 1.22+, WezTerm,
  kitty) shows them in place; elsewhere you get the path, to copy the file with `scp`. The files are
  removed when you quit, because they show participants' brains.
- **First-level stats:** type an MNI position (`-8 -92 4`) to move a run's cursor, instead of
  clicking.
- **MRIQC's HTML reports** are in the MRIQC output folder; the metrics and ratings are in the tab.
- **Docker** has to be running: `sudo systemctl start docker` on a server.
- **Settings** are in `~/.config/fmri_local_process/`, apart from the window's.

## Quitting while something runs

- **C-PAC:** stop it (finished steps are kept) or leave the running containers going. The rest
  of a batch doesn't start once the app is closed, so for long batches stay in `tmux`.
- **MRIQC:** it is stopped; the subject it was on starts afresh next time.
- **Converting or trimming:** stopped; the scan it was on is done again next time.

## Installing

From the repo: `pip install -e ".[tui]"` (no Qt). For a server without
Python, use [the Linux bundle](linux-bundle.md).
