"""Without Qt (a headless server's environment) the window's tests are left out."""

from importlib.util import find_spec

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
