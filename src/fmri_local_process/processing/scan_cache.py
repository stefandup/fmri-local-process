"""What was read from the behaviour logs and DICOM session folders, kept between launches.

Reading every log and walking every session folder is slow on a big data server, so the
results are kept per user (outside the data folders) and reused for each log or folder that
hasn't changed since. A new version of the toolbox starts the cache afresh. Delete the file to
force a full re-read.
"""

import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
from typing import Any

from fmri_local_process.processing.records import write_json_atomic

log = logging.getLogger(__name__)

CACHE_DIR_ENV = "FMRI_CACHE_DIR"  # overrides where the cache goes (the tests use it)
PACKAGE_NAME = "fmri-local-process"


def default_cache_dir() -> Path:
    if override := os.environ.get(CACHE_DIR_ENV):
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "fmri_local_process" / "scan"


def toolbox_version() -> str:
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return ""


def file_stamp(path: Path) -> list[int]:
    """[size, modification time]: changes when the file is written."""
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def folder_stamp(folder: Path) -> list[Any]:
    """The folder's modification time and that of each folder directly in it (the series):
    adding a series, or a file to a series, changes it -- without listing every DICOM file."""
    with os.scandir(folder) as entries:
        children = sorted([e.name, e.stat().st_mtime_ns] for e in entries if e.is_dir())
    return [folder.stat().st_mtime_ns, children]


class ScanCache:
    """One JSON file per (behaviour folder, DICOM folder). `logs` and `dicom` are plain dicts
    that inspect_logs and scan_dicom_root read and update."""

    def __init__(self, path: Path | None, version: str) -> None:
        self.path, self.version = path, version
        self.logs: dict[str, Any] = {}
        self.dicom: dict[str, Any] = {}

    @classmethod
    def open(cls, behav_root: Path, dicom_root: Path, cache_dir: Path | None = None) -> "ScanCache":
        """The cache for these folders; empty when there is none, it's unreadable or it's from
        another version. Without a known version nothing is kept (it could be stale)."""
        version = toolbox_version()
        if not version:
            return cls(None, version)
        roots = f"{behav_root.resolve()}|{dicom_root.resolve()}"
        name = hashlib.sha256(roots.encode()).hexdigest()[:16]
        path = (cache_dir or default_cache_dir()) / f"{name}.json"
        cache = cls(path, version)
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cache
        if isinstance(stored, dict) and stored.get("version") == version:
            cache.logs = stored.get("logs") or {}
            cache.dicom = stored.get("dicom") or {}
        return cache

    def save(self) -> None:
        """Write it; a cache that can't be written is only logged."""
        if self.path is None:
            return
        try:
            write_json_atomic(
                self.path, {"version": self.version, "logs": self.logs, "dicom": self.dicom}
            )
        except OSError as e:
            log.warning("Can't write the scan cache %s: %s", self.path, e)
