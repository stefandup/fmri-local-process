"""Debug harness for step 4: the two guards in test_toy_4_guards.py, worked through by hand.

Not a test and not part of the app. Run it from Run and Debug -> "Debug toy step 4".
Without the debugger it just prints.

Guard 1: the terminal app must not load Qt. The test starts a fresh Python for this, because
the test process has Qt loaded already. This harness *is* a fresh Python, so it can check
directly: look at sys.modules (every module loaded so far) before and after each import.
That is why nothing from the toy is imported at the top of this file.

Guard 2: every tab in TAB_ORDER has a page in both UIs.
"""

import importlib
import sys
from types import ModuleType


def loaded_qt_modules() -> list[str]:
    """The names of all loaded PySide6 modules, from sys.modules."""
    qt_module_names: list[str] = []
    for module_name in sorted(sys.modules):
        if module_name.startswith("PySide6"):
            qt_module_names.append(module_name)
    return qt_module_names


def main() -> None:
    # ---- Guard 1: the TUI doesn't load Qt ---------------------------------------------------
    qt_before_anything: list[str] = loaded_qt_modules()

    toy_tui: ModuleType = importlib.import_module("toy_tui")  # same as `import toy_tui`
    qt_after_tui_import: list[str] = loaded_qt_modules()
    tui_is_qt_free: bool = len(qt_after_tui_import) == 0

    toy_gui: ModuleType = importlib.import_module("toy_gui")  # for contrast: the Qt window
    qt_after_gui_import: list[str] = loaded_qt_modules()

    # ---- Guard 2: every tab exists in both UIs ----------------------------------------------
    toy_app: ModuleType = importlib.import_module("toy_app")
    tabs_in_order: set[str] = set(toy_app.TAB_ORDER)
    gui_pages: set[str] = set(toy_gui.PAGES)
    tui_pages: set[str] = set(toy_tui.PAGES)
    missing_from_gui: set[str] = tabs_in_order - gui_pages
    missing_from_tui: set[str] = tabs_in_order - tui_pages

    # ---- What happened ----------------------------------------------------------------------
    print(f"Qt modules before anything: {qt_before_anything}")
    print(f"Qt modules after toy_tui:   {qt_after_tui_import}  (Qt-free: {tui_is_qt_free})")
    print(f"Qt modules after toy_gui:   {len(qt_after_gui_import)} loaded")
    print(f"tab order:                  {toy_app.TAB_ORDER}")
    print(f"missing from GUI / TUI:     {missing_from_gui} / {missing_from_tui}")


if __name__ == "__main__":
    main()
