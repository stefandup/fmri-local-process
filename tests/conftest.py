"""Without Qt (a headless server's environment) the window's tests are left out. No test sees the
user's own remembered folders."""

from importlib.util import find_spec
from pathlib import Path

import pytest

if find_spec("PySide6") is None:
    collect_ignore = [
        "test_behav_logs_dialog.py",
        "test_crosscheck_gui.py",
        "test_first_level_gui.py",
        "test_launcher.py",
        "test_mriqc_page.py",
        "test_preprocess_gui.py",
        "toy_app_layer/test_toy_0_plain_gui.py",
        "toy_app_layer/test_toy_2_gui.py",
        "toy_app_layer/test_toy_4_guards.py",
    ]


@pytest.fixture(autouse=True)
def own_settings(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path_factory.mktemp("config")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(folder))
    return folder
