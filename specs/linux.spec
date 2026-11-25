# Builds the terminal app and every command-line tool as one onedir bundle, so they share a single
# copy of their dependencies (build.sh --tui). As vr_lab_toolbox's specs/toolbox.spec. No Qt.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

REPO_ROOT = os.path.join(SPECPATH, "..")
SRC_ROOT = os.path.join(REPO_ROOT, "src", "fmri_local_process")
DCM2NIIX_DIR = os.environ["DCM2NIIX_DIR"]

# (command, script path relative to src/fmri_local_process)
TOOLS = [
    ("fmri_tui", os.path.join("tui", "toolbox.py")),
    ("fmri_convert_to_bids", os.path.join("cli", "convert_to_bids.py")),
    ("fmri_preprocess", os.path.join("cli", "preprocess.py")),
    ("fmri_mriqc", os.path.join("cli", "mriqc.py")),
    ("fmri_behav_events", os.path.join("cli", "behav_events.py")),
    ("fmri_behav_check", os.path.join("cli", "behav_check.py")),
    ("fmri_bids_check", os.path.join("cli", "bids_check.py")),
    ("fmri_generate_sample_data", os.path.join("cli", "generate_sample_data.py")),
]

collected = []
for name, script in TOOLS:
    analysis = Analysis(
        [os.path.join(SRC_ROOT, script)],
        pathex=[os.path.join(REPO_ROOT, "src")],
        binaries=[(os.path.join(DCM2NIIX_DIR, "dcm2niix"), "dcm2niix")],
        datas=[
            (os.path.join(DCM2NIIX_DIR, "license.txt"), "dcm2niix"),
            *copy_metadata("fmri-local-process"),
            *collect_data_files("textual"),
        ],
        # Textual loads its widgets by name, when they are first used.
        hiddenimports=(
            [*collect_submodules("textual.widgets"), *collect_submodules("textual_image")]
            if name == "fmri_tui"
            else []
        ),
        runtime_hooks=[os.path.join(SPECPATH, "linux_environment_hook.py")],
        excludes=["PySide6", "PyQt5", "PyQt6", "tkinter", "IPython", "pytest"],
    )
    exe = EXE(
        PYZ(analysis.pure),
        analysis.scripts,
        # UTF-8 also where the server's locale is plain C / POSIX.
        [("X utf8_mode=1", None, "OPTION")],
        exclude_binaries=True,
        name=name,
        console=True,
    )
    collected += [exe, analysis.binaries, analysis.datas]

COLLECT(*collected, name="fmri_tui")
