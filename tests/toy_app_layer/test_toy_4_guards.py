"""Step 4: the two checks that keep the window and the terminal app in step later on."""

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("textual")

import toy_gui  # noqa: E402
import toy_tui  # noqa: E402
from toy_app import TAB_ORDER  # noqa: E402

HERE = Path(__file__).parent


def test_every_tab_exists_in_both_uis() -> None:
    """Add a tab to TAB_ORDER and the GUI only, and this fails until the TUI has it too.
    (The real version allows an explicit 'not yet in the TUI' list.)"""
    assert set(toy_gui.PAGES) == set(TAB_ORDER)
    assert set(toy_tui.PAGES) == set(TAB_ORDER)


def test_tui_never_loads_qt() -> None:
    """In a fresh Python (this test process has Qt loaded already), import the TUI and check
    that PySide6 didn't come along. On a headless server Qt may not even be installed."""
    check = "import sys, toy_tui; sys.exit(any(m.startswith('PySide6') for m in sys.modules))"
    result = subprocess.run([sys.executable, "-c", check], cwd=HERE)
    assert result.returncode == 0, "importing toy_tui loaded PySide6"
