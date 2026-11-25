# PyInstaller spec of the Linux bundle (build.sh --tui): the terminal app and the command-line tools
# in one folder, sharing their libraries. No Qt.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parents[1]
PACKAGE = ROOT / "src" / "fmri_local_process"
DCM2NIIX = Path(os.environ["DCM2NIIX_DIR"])
SCRIPTS = {
    "fmri_tui": PACKAGE / "tui" / "toolbox.py",
    "fmri_convert_to_bids": PACKAGE / "cli" / "convert_to_bids.py",
    "fmri_preprocess": PACKAGE / "cli" / "preprocess.py",
    "fmri_behav_events": PACKAGE / "cli" / "behav_events.py",
    "fmri_behav_check": PACKAGE / "cli" / "behav_check.py",
}

exes, parts = [], []
for name, script in SCRIPTS.items():
    analysis = Analysis(
        [str(script)],
        pathex=[str(ROOT / "src")],
        binaries=[(str(DCM2NIIX / "dcm2niix"), "dcm2niix")],
        datas=[
            (str(DCM2NIIX / "license.txt"), "dcm2niix"),
            *collect_data_files("textual"),
            *copy_metadata("fmri-local-process"),
        ],
        # Textual loads its widgets by name, when they are first used.
        hiddenimports=[*collect_submodules("textual.widgets"), *collect_submodules("textual_image")],
        runtime_hooks=[str(ROOT / "packaging" / "linux" / "rthook_environment.py")],
        excludes=["PySide6", "PyQt5", "PyQt6", "tkinter", "IPython", "pytest"],
    )
    exes.append(
        EXE(
            PYZ(analysis.pure),
            analysis.scripts,
            # UTF-8 also where the server's locale is plain C / POSIX.
            [("X utf8_mode=1", None, "OPTION")],
            exclude_binaries=True,
            name=name,
            console=True,
        )
    )
    parts += [analysis.binaries, analysis.datas]

COLLECT(*exes, *parts, name="fmri_tui")
