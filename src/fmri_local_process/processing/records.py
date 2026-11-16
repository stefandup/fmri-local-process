"""Writing the crosscheck's record files (corrections, decisions, notes, settings) safely.

The JSON is written to a temporary file beside the target and then swapped in, so a crash
or a full disk mid-write leaves the previous file intact rather than a half-written one that
no longer parses (vr_lab_toolbox's `_write_json_atomic`).
"""

import json
import os
import tempfile
from pathlib import Path


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as tmp:
        json.dump(value, tmp, indent=2)
        tmp.write("\n")
        name = tmp.name
    try:
        os.replace(name, path)
    except OSError:
        Path(name).unlink(missing_ok=True)
        raise
