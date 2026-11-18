"""Synthetic data for the toy: a folder with two small text files (and one that isn't .txt).
Used by the tests and by the runnable demos."""

from pathlib import Path


def make_folder(parent: Path) -> Path:
    """Create parent/notes with a.txt (2 lines), b.txt (3 lines) and skip.csv; return it."""
    folder = parent / "notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.txt").write_text("one\ntwo\n")
    (folder / "b.txt").write_text("one\ntwo\nthree\n")
    (folder / "skip.csv").write_text("not counted\n")
    return folder
