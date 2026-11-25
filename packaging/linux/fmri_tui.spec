# PyInstaller spec of the Linux bundle (build.sh --tui): the terminal app and the command-line tools
# as one program (entry.py; the other commands are links to it). No Qt.
import os
import runpy
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

HERE = Path(SPECPATH)
DCM2NIIX = Path(os.environ["DCM2NIIX_DIR"])
COMMANDS = runpy.run_path(str(HERE / "entry.py"))["COMMANDS"]

analysis = Analysis(
    [str(HERE / "entry.py")],
    pathex=[str(HERE.parents[1] / "src")],
    binaries=[(str(DCM2NIIX / "dcm2niix"), "dcm2niix")],
    datas=[
        (str(DCM2NIIX / "license.txt"), "dcm2niix"),
        *collect_data_files("textual"),
        *copy_metadata("fmri-local-process"),
    ],
    # entry.py imports the commands by name, and Textual its widgets when first used.
    hiddenimports=[
        *COMMANDS.values(),
        *collect_submodules("textual.widgets"),
        *collect_submodules("textual_image"),
    ],
    runtime_hooks=[str(HERE / "rthook_environment.py")],
    excludes=["PySide6", "PyQt5", "PyQt6", "tkinter", "IPython", "pytest"],
)
exe = EXE(
    PYZ(analysis.pure),
    analysis.scripts,
    # UTF-8 also where the server's locale is plain C / POSIX.
    [("X utf8_mode=1", None, "OPTION")],
    exclude_binaries=True,
    name="fmri_tui",
    console=True,
)
COLLECT(exe, analysis.binaries, analysis.datas, name="fmri_tui")
