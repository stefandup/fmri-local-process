"""Where remembered choices go: the window plugs in QSettings (the registry on Windows), the
terminal app one JSON file per app (a headless server has no registry)."""

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from fmri_local_process.processing.review_mode import package_root, settings_dir

CONFIG_FOLDER = "fmri_local_process"


class Settings(Protocol):
    """What the shared code needs from a settings store; QSettings fits as it is. (The `/`
    makes the arguments positional, so QSettings' other argument names don't matter.)"""

    def value(self, key: str, default: Any = None, /) -> Any: ...
    def setValue(self, key: str, value: Any, /) -> None: ...  # noqa: N802 -- Qt's spelling
    def remove(self, key: str, /) -> None: ...
    def sync(self) -> None: ...


MakeSettings = Callable[[str], Settings]  # app name -> its settings store


class JsonSettings:
    """One JSON file, written on every change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = self._read()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # not there yet, or damaged: start afresh
            return {}
        return data if isinstance(data, dict) else {}

    def value(self, key: str, default: Any = None, /) -> Any:
        return self.data.get(key, default)

    def setValue(self, key: str, value: Any, /) -> None:  # noqa: N802 -- Qt's spelling
        self.data = self._read() | {key: value}  # another program may have written meanwhile
        self.sync()

    def remove(self, key: str, /) -> None:
        self.data = self._read()
        if self.data.pop(key, None) is not None:
            self.sync()

    def sync(self) -> None:
        """Written to a temporary file first: a full disk never leaves half a file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False
        ) as f:
            json.dump(self.data, f, indent=2, default=str)
        Path(f.name).replace(self.path)


def config_dir() -> Path:
    """~/.config/fmri_local_process, or the review package's own settings folder."""
    if (root := package_root()) is not None:
        return settings_dir(root)
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / CONFIG_FOLDER


def json_settings(app: str) -> JsonSettings:
    """The terminal app's settings store for app."""
    return JsonSettings(config_dir() / f"{app}.json")


def is_on(value: object) -> bool:
    """A remembered tick: QSettings may give 'true' back as text."""
    return str(value).lower() == "true"


def saved_number(settings: Settings, key: str, default: float) -> float:
    try:
        return float(str(settings.value(key, default)))
    except ValueError:
        return default
