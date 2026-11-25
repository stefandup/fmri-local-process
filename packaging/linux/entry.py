"""The Linux bundle's one program: it runs the command whose name it was started by (the commands
are links to it), so they share one copy of everything."""

import importlib
import sys
from pathlib import Path

COMMANDS = {
    "fmri_tui": "fmri_local_process.tui.toolbox",
    "fmri_convert_to_bids": "fmri_local_process.cli.convert_to_bids",
    "fmri_preprocess": "fmri_local_process.cli.preprocess",
    "fmri_behav_events": "fmri_local_process.cli.behav_events",
    "fmri_behav_check": "fmri_local_process.cli.behav_check",
    "fmri_behav_fix": "fmri_local_process.cli.behav_fix",
    "fmri_bids_check": "fmri_local_process.cli.bids_check",
    "fmri_bids_fix": "fmri_local_process.cli.bids_fix",
    "fmri_mriqc": "fmri_local_process.cli.mriqc",
    "fmri_folders": "fmri_local_process.cli.folders",
    "fmri_generate_sample_data": "fmri_local_process.cli.generate_sample_data",
}

if __name__ == "__main__":
    name = Path(sys.argv[0]).name
    if name not in COMMANDS:
        sys.exit(f"Unknown command {name}: one of {', '.join(COMMANDS)}")
    importlib.import_module(COMMANDS[name]).main()
