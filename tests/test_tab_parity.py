"""The window and the terminal app show the same tabs: one added to only one of them fails
here (the terminal app may be behind on those listed in NOT_YET_IN_TUI)."""

from importlib.util import find_spec

import pytest

from fmri_local_process.app.tabs import NOT_YET_IN_TUI, TAB_INTROS, TAB_ORDER


def test_the_list_of_missing_tabs_names_real_ones() -> None:
    assert set(NOT_YET_IN_TUI) <= set(TAB_ORDER)
    assert set(TAB_INTROS) == set(TAB_ORDER)


@pytest.mark.skipif(
    find_spec("fmri_local_process.tui") is None or find_spec("textual") is None,
    reason="no terminal app here",
)
def test_every_tab_is_in_the_terminal_app_or_listed_as_missing() -> None:
    from fmri_local_process.tui.toolbox import PAGES  # pyright: ignore[reportMissingImports]

    assert set(PAGES) | set(NOT_YET_IN_TUI) == set(TAB_ORDER)
    assert not set(PAGES) & set(NOT_YET_IN_TUI)  # done: take it off the list
