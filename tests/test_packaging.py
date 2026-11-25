"""The packaging scripts name the images and commands the code uses."""

import runpy
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from fmri_local_process.processing.cpac_docker import CPAC_IMAGE
from fmri_local_process.processing.deface import DEFACE_IMAGE
from fmri_local_process.processing.mriqc import MRIQC_IMAGE

ROOT = Path(__file__).parents[1]
SETUP = ROOT / "packaging" / "docker" / "setup_docker_image.sh"
WINDOW_COMMANDS = {"fmri_toolbox", "fmri_crosscheck", "fmri_preprocess_gui"}  # need Qt


def test_linux_docker_setup_uses_the_codes_images() -> None:
    text = SETUP.read_text(encoding="utf-8")
    for image in (CPAC_IMAGE, DEFACE_IMAGE, MRIQC_IMAGE):
        assert f":-{image}}}" in text


def test_linux_bundle_holds_every_command_but_the_windows() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entry = runpy.run_path(str(ROOT / "packaging" / "linux" / "entry.py"))
    expected = {
        name: target.removesuffix(":main")
        for name, target in project["project"]["scripts"].items()
        if name not in WINDOW_COMMANDS
    }
    assert entry["COMMANDS"] == expected


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("args", [[], ["--defacer", "x"], ["--defacer", "pydeface", "--no-cpac"]])
def test_linux_docker_setup_refuses_wrong_options(args: list[str]) -> None:
    result = subprocess.run(["bash", str(SETUP), *args], capture_output=True, text=True)
    assert result.returncode == 1 and "ERROR" in result.stderr
