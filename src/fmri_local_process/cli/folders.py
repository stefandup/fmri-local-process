"""fmri_folders: the folders every command works on, set once (and shared with the terminal app), so
they needn't be typed each time. Also how the other commands find them."""

from pathlib import Path

import click

from fmri_local_process.app.settings import config_dir, json_settings
from fmri_local_process.app.state import FOLDER_KEYS, ToolboxState, folder_conflict

NAMES = {
    "dicom": "DICOM sessions",
    "behav": "behavioural logs",
    "bids": "BIDS",
    "output": "C-PAC output",
    "mriqc": "MRIQC output",
}
MADE_IF_MISSING = ("bids", "output", "mriqc")


def find_folders(
    required: dict[str, Path | None], optional: dict[str, Path | None] | None = None
) -> tuple[dict[str, Path], dict[str, Path | None], bool]:
    """(the required folders, the optional ones, whether they are the remembered ones): what was
    given, or, when nothing was, what fmri_folders remembers. Never a mix: one study's logs
    with another's BIDS folder."""
    optional = optional or {}
    given = any(folder is not None for folder in (*required.values(), *optional.values()))
    if given:
        if missing := [NAMES[k] for k, folder in required.items() if folder is None]:
            raise click.UsageError(
                f"Give the {' and '.join(missing)} folder too, or none (then the ones set with "
                "fmri_folders are used)."
            )
        return {k: f for k, f in required.items() if f is not None}, optional, False
    state = ToolboxState(json_settings)
    found = {key: state.folder(key) for key in required}
    if missing := [key for key, folder in found.items() if folder is None]:
        flags = " ".join(f"--{key} <folder>" for key in missing)
        raise click.UsageError(f"No folder given. Set it once with:  fmri_folders {flags}")
    folders = {key: folder for key, folder in found.items() if folder is not None}
    extra = {key: state.folder(key) for key in optional}
    for key, folder in (folders | {k: f for k, f in extra.items() if f is not None}).items():
        click.echo(f"{NAMES[key]}: {folder}", err=True)
    return folders, extra, True


def folder_option(key: str):  # noqa: ANN201 -- click's decorator
    """--<key>: only needed for other folders than the remembered ones."""
    kind = click.Path(exists=True, file_okay=False, path_type=Path)
    text = f"{NAMES[key]} folder  [default: the one set with fmri_folders]"
    return click.option(f"--{key}", f"{key}_root", type=kind, help=text)


@click.command(
    epilog="""
Examples:

\b
  fmri_folders --dicom D:\\philani --behav D:\\philani_logs --bids D:\\philani_bids
  fmri_folders                     which folders are set
  fmri_folders --clear             forget them
"""
)
@click.version_option(package_name="fmri-local-process")
@click.option("--dicom", type=click.Path(path_type=Path), help="DICOM session folders (raw).")
@click.option("--behav", type=click.Path(path_type=Path), help="Behavioural logs (raw).")
@click.option("--bids", type=click.Path(path_type=Path), help="BIDS folder.")
@click.option("--output", type=click.Path(path_type=Path), help="C-PAC output folder.")
@click.option("--mriqc", type=click.Path(path_type=Path), help="MRIQC output folder.")
@click.option("--clear", is_flag=True, help="Forget every folder.")
@click.option("--yes", "-y", is_flag=True, help="Make missing folders without asking.")
def main(clear: bool, yes: bool, **given: Path | None) -> None:
    """Set the folders the commands (and fmri_tui) work on, or show them.

    After this, the commands need no folders: fmri_behav_check, fmri_bids_check, ...
    """
    state = ToolboxState(json_settings)
    if clear:
        for key in FOLDER_KEYS:
            state.set_folder(key, None)
    for key, folder in given.items():
        if folder is not None:
            set_folder(state, key, folder.expanduser().absolute(), yes)
    for key in FOLDER_KEYS:
        click.echo(f"{key:7} {state.folder(key) or '-'}")
    click.echo(f"(remembered in {config_dir()})", err=True)


def set_folder(state: ToolboxState, key: str, folder: Path, yes: bool) -> None:
    if why := folder_conflict(state, key, folder):
        raise click.ClickException(f"Not {folder} for {key}: {why}.")
    if not folder.is_dir():
        if folder.exists() or key not in MADE_IF_MISSING:
            raise click.ClickException(f"There is no folder {folder}.")
        if not yes:
            click.confirm(f"{folder} does not exist. Make it?", abort=True)
        folder.mkdir(parents=True)
    state.set_folder(key, folder)


if __name__ == "__main__":
    main()
