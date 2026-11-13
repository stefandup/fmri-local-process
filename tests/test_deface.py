"""Tests for the defacer lookup and its docker commands (no FreeSurfer or Docker needed)."""

import sys
from pathlib import Path

import pytest

from fmri_local_process.processing import deface as df


def make_files(folder: Path, freesurfer_layout: bool) -> None:
    exe_dir = folder / "bin" if freesurfer_layout else folder
    template_dir = folder / "average" if freesurfer_layout else folder
    for path in (
        exe_dir / "mri_deface",
        template_dir / df.BRAIN_TEMPLATE,
        template_dir / df.FACE_TEMPLATE,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")


@pytest.mark.parametrize("freesurfer_layout", [True, False])
def test_find_mri_deface_files_in_both_layouts(tmp_path: Path, freesurfer_layout: bool) -> None:
    make_files(tmp_path, freesurfer_layout)
    files = df.find_mri_deface_files(tmp_path)
    assert files is not None
    assert files.exe.name == "mri_deface"
    assert files.face_template.name == df.FACE_TEMPLATE


def test_find_mri_deface_files_needs_all_three(tmp_path: Path) -> None:
    make_files(tmp_path, freesurfer_layout=False)
    (tmp_path / df.FACE_TEMPLATE).unlink()
    assert df.find_mri_deface_files(tmp_path) is None


def test_explicit_folder_without_files_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(df.DefaceUnavailableError, match="holds no mri_deface"):
        df.make_deface(tmp_path)


def test_docker_command_with_image_mri_deface(tmp_path: Path) -> None:
    t1w = tmp_path / "sub-1_T1w.nii.gz"
    cmd = df.build_docker_deface_command(Path("docker"), "img", t1w, df.MRI_DEFACE)
    entry = cmd.index("--entrypoint")
    assert cmd[entry + 1 :] == [
        "/opt/mri_deface/mri_deface",
        "img",
        "/data/sub-1_T1w.nii.gz",
        f"/opt/mri_deface/{df.BRAIN_TEMPLATE}",
        f"/opt/mri_deface/{df.FACE_TEMPLATE}",
        "/data/sub-1_T1w_defaced.nii.gz",
    ]
    assert f"{tmp_path.resolve()}:/data" in cmd


def test_docker_command_with_mounted_freesurfer_home(tmp_path: Path) -> None:
    make_files(tmp_path / "fs", freesurfer_layout=True)
    files = df.find_mri_deface_files(tmp_path / "fs")
    assert files is not None
    cmd = df.build_docker_deface_command(
        Path("docker"), "img", tmp_path / "t.nii.gz", df.MRI_DEFACE, files
    )
    assert f"{(tmp_path / 'fs').resolve()}:{df.TOOLS_MOUNT}:ro" in cmd
    assert cmd[cmd.index("--entrypoint") + 1] == f"{df.TOOLS_MOUNT}/bin/mri_deface"
    assert f"{df.TOOLS_MOUNT}/average/{df.FACE_TEMPLATE}" in cmd


def test_docker_command_with_pydeface(tmp_path: Path) -> None:
    cmd = df.build_docker_deface_command(Path("docker"), "img", tmp_path / "t.nii.gz", df.PYDEFACE)
    assert cmd[cmd.index("--entrypoint") :] == [
        "--entrypoint",
        "pydeface",
        "img",
        "/data/t.nii.gz",
        "--outfile",
        "/data/t_defaced.nii.gz",
        "--force",
    ]


def test_run_defacer_replaces_original_and_drops_log(tmp_path: Path) -> None:
    t1w = tmp_path / "t.nii.gz"
    t1w.write_bytes(b"face")
    defaced = df.defaced_path(t1w)
    script = f"open(r'{defaced}', 'wb').write(b'no face'); open(r'{defaced}.log', 'w').write('log')"
    assert df.run_defacer([sys.executable, "-c", script], t1w, "tool") == "tool"
    assert t1w.read_bytes() == b"no face"
    assert list(tmp_path.iterdir()) == [t1w]


def test_run_defacer_failure_keeps_original(tmp_path: Path) -> None:
    t1w = tmp_path / "t.nii.gz"
    t1w.write_bytes(b"face")
    script = "import sys; sys.exit('registration failed')"
    with pytest.raises(df.DefaceFailedError, match="registration failed"):
        df.run_defacer([sys.executable, "-c", script], t1w, "tool")
    assert t1w.read_bytes() == b"face"
