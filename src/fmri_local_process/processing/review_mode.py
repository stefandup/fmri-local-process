"""Is this app running from a review package? (see processing/review_package.py)

A review package holds the toolbox with a chosen part of the data. Its MANIFEST sits beside
the exe (or in the folder FMRI_REVIEW_PACKAGE names, for trying a package from source). Then:
* the folders start on the packaged data; one chosen in the app wins while it exists
* the app's settings live in the package (review_data/settings), not in the registry
* the first-level atlases come from the package (review_data/atlases): no Docker needed
* a view-only package hides Setup and Preprocess and locks every changing action

Kept free of heavy imports: the settings and the atlas cache ask it at startup.
"""

import json
import os
import sys
from pathlib import Path

MANIFEST = "review_package.json"
DATA_DIR = "review_data"
ENV_VAR = "FMRI_REVIEW_PACKAGE"


def package_root() -> Path | None:
    """The review package this app runs from, or None for a normal install."""
    if env := os.environ.get(ENV_VAR):
        return Path(env) if (Path(env) / MANIFEST).is_file() else None
    if getattr(sys, "frozen", False):  # a PyInstaller exe: the package is its folder
        folder = Path(sys.executable).parent
        if (folder / MANIFEST).is_file():
            return folder
    return None


def read_manifest(root: Path) -> dict:
    try:
        value = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def package_folders(root: Path | None = None) -> dict[str, Path]:
    """{folder key: packaged folder} for the folders the package holds."""
    root = root or package_root()
    if root is None:
        return {}
    folders = read_manifest(root).get("folders", {})
    return {
        str(key): root / str(rel)
        for key, rel in folders.items()
        if isinstance(rel, str) and (root / rel).is_dir()
    }


def settings_dir(root: Path) -> Path:
    return root / DATA_DIR / "settings"


def atlas_dir(root: Path | None = None) -> Path | None:
    """The packaged atlas cache, if this is a review package that has one."""
    root = root or package_root()
    if root is None:
        return None
    folder = root / DATA_DIR / "atlases"
    return folder if folder.is_dir() else None


def view_only(root: Path | None = None) -> bool:
    """A view-only package: the toolbox hides Setup and Preprocess and locks changes."""
    root = root or package_root()
    return bool(read_manifest(root).get("view_only", False)) if root else False


def package_name(root: Path | None = None) -> str:
    root = root or package_root()
    return str(read_manifest(root).get("name", "")) if root else ""
