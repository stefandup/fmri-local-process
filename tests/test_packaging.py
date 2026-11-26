"""The packaging scripts name the images and commands the code uses."""

import os
import re
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
    spec = (ROOT / "specs" / "linux.spec").read_text(encoding="utf-8")
    tools = set(re.findall(r'\("(fmri_\w+)", os\.path\.join\("\w+", "(\w+)\.py"\)\)', spec))
    expected = {
        (name, target.removesuffix(":main").rsplit(".", 1)[-1])
        for name, target in project["project"]["scripts"].items()
        if name not in WINDOW_COMMANDS
    }
    assert tools == expected


# Not on Windows: there `bash` is usually WSL's, which can't read a Windows path.
@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="needs Linux bash")
@pytest.mark.parametrize("args", [[], ["--defacer", "x"], ["--defacer", "pydeface", "--no-cpac"]])
def test_linux_docker_setup_refuses_wrong_options(args: list[str]) -> None:
    result = subprocess.run(["bash", str(SETUP), *args], capture_output=True, text=True)
    assert result.returncode == 1 and "ERROR" in result.stderr


def test_windows_installer_holds_every_command_line_tool() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build = (ROOT / "build.ps1").read_text(encoding="utf-8")
    tools = set(re.findall(r'"(fmri_\w+)"\s*= Join-Path \$CliDir "(\w+)\.py"', build))
    assert tools == {
        (name, target.removesuffix(":main").rsplit(".", 1)[-1])
        for name, target in project["project"]["scripts"].items()
        if ".cli." in target
    }


# ---- the Linux installer ------------------------------------------------------------------------

needs_linux = pytest.mark.skipif(os.name == "nt", reason="the Linux installer")


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """An unpacked bundle with one made-up command."""
    app = tmp_path / "package" / "app"
    (app / "_internal").mkdir(parents=True)
    (app / "fmri_x").write_text("#!/bin/sh\necho it works\n")
    (app / "fmri_x").chmod(0o755)
    (app / "commands.txt").write_text("fmri_x\n")
    shutil.copy(ROOT / "packaging" / "install.sh", app.parent)
    (tmp_path / "home").mkdir()
    return app.parent


def shell(package: Path, *command: str, ok: bool = True) -> str:
    env = {"HOME": str(package.parent / "home"), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash"}
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == ok, result.stderr
    return result.stdout + result.stderr


@needs_linux
@pytest.mark.parametrize("folder", [None, "my $tools"])
def test_the_toolbox_folder_is_put_on_path_once(package: Path, folder: str | None) -> None:
    home = package.parent / "home"
    (home / ".bashrc").write_text("alias ll='ls -l'\n")
    toolbox = home / folder if folder else home / ".local" / "opt" / "fmri_local_process"
    args = [str(toolbox)] if folder else []
    install = str(package / "install.sh")
    for _ in range(2):  # the second is an upgrade
        (toolbox / "old_tool").touch() if toolbox.exists() else None
        shell(package, install, *args)
    assert not (toolbox / "old_tool").exists()
    for start in (["bash", "-lc"], ["bash", "-ic"], ["sh", "-lc"]):
        assert "it works" in shell(package, *start, "fmri_x")
        assert shell(package, *start, "echo $PATH").count(str(toolbox)) == 1
    shell(package, install, "--uninstall", *args)
    assert not toolbox.exists() and not (home / ".profile").exists()
    assert (home / ".bashrc").read_text() == "alias ll='ls -l'\n"


@needs_linux
def test_only_a_toolbox_folder_is_ever_removed(package: Path) -> None:
    other = package.parent / "home" / "data"
    other.mkdir()
    (other / "precious.txt").write_text("x")
    for args in ([], ["--uninstall"]):
        assert "something else" in shell(
            package, str(package / "install.sh"), *args, str(other), ok=False
        )
    assert (other / "precious.txt").exists()


@needs_linux
def test_the_earlier_layout_is_replaced_and_the_backups_kept(package: Path) -> None:
    home = package.parent / "home"
    old, launchers = home / ".local/share/fmri_local_process", home / ".local/bin"
    backups = home / ".local/share/FmriLocalProcess/crosscheck_backups"
    for folder in (old / "_internal", launchers, backups):
        folder.mkdir(parents=True)
    (launchers / "fmri_x").write_text(f'#!/usr/bin/env bash\nexec "{old}/fmri_x" "$@"\n')
    (launchers / "fmri_mine").write_text("#!/bin/sh\n")  # not the installer's
    shell(package, str(package / "install.sh"))
    assert not old.exists() and backups.exists()
    assert [p.name for p in launchers.iterdir()] == ["fmri_mine"]
