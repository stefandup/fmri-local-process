"""Assemble a review package from a plan the picker saved (build.ps1 -Review runs this).

Thin wrapper around processing/review_package.py.
"""

from pathlib import Path

import click

from fmri_local_process.processing.review_package import Plan, ReviewPackageError, assemble


@click.command(
    epilog="""
Example (build.ps1 -Review does this after building the app):

\b
  python -m fmri_local_process.cli.review_package build_output\\review\\plan.json
      --app build_output\\dist\\fmri_toolbox --out build_output\\review
"""
)
@click.argument("plan_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--app",
    "app_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="The built app folder to put in the package (left out if not given).",
)
@click.option(
    "--out",
    "out_root",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Folder the package folder (and .zip) is made in.",
)
def main(plan_file: Path, app_dir: Path | None, out_root: Path) -> None:
    """Copy the app and the planned data into OUT/<package name>."""
    plan = Plan.from_json(plan_file.read_text(encoding="utf-8"))
    try:
        assemble(plan, out_root, app_dir, progress=click.echo)
    except (ReviewPackageError, OSError) as e:
        raise click.ClickException(str(e)) from e


if __name__ == "__main__":
    main()
