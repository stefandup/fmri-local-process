"""Runs before the app in the Linux bundle: the programs it starts (docker, dcm2niix, mri_deface,
Rscript) must find the server's own libraries, not the bundle's."""

import os

original = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
if original is None:
    os.environ.pop("LD_LIBRARY_PATH", None)  # set by the bundle's own starter
else:
    os.environ["LD_LIBRARY_PATH"] = original
