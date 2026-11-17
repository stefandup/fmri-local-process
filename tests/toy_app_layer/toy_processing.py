"""Layer 1, processing/: the actual work. Plain functions: no UI, no threads, no state.

In the real project this layer already exists and doesn't change.
"""

from pathlib import Path


def text_files(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.txt"))


def count_lines(path: Path) -> int:
    with path.open(encoding="utf-8") as file:
        return sum(1 for _ in file)
