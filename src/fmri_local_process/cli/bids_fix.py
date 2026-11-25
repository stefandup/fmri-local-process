"""fmri_bids_fix: the crosscheck's decisions on a BIDS folder, as the BIDS Crosscheck and Scan
lengths tabs make them. Whatever deletes or trims asks first (--yes: don't ask), and every change is
in the folder's action log."""

from collections.abc import Callable
from pathlib import Path

import click

from fmri_local_process.app import backup
from fmri_local_process.app.crosscheck import delete_question, delete_sessions
from fmri_local_process.app.trimming import TrimJob, confirm_text
from fmri_local_process.cli.folders import find_folders, folder_option
from fmri_local_process.processing import crosscheck as cc
from fmri_local_process.processing.crosscheck_backup import save_backup

EPILOG = """
Examples, for the folder set with fmri_folders (sessions and files as fmri_bids_check lists them):

\b
  fmri_bids_fix mark sub-P001_ses-1 sub-P002_ses-1
  fmri_bids_fix note sub-P001_ses-1 "moved a lot"
  fmri_bids_fix keep sub-P001_ses-1_task-Rest_run-2_bold.nii.gz
  fmri_bids_fix expect task-Rest_bold 495
  fmri_bids_fix trim
"""
yes_option = click.option("--yes", "-y", is_flag=True, help="Don't ask.")


@click.group(epilog=EPILOG)
@click.version_option(package_name="fmri-local-process")
def main() -> None:
    """Record the crosscheck's decisions on a BIDS folder."""


def chosen(bids_root: Path, names: tuple[str, ...]) -> list[cc.SessionReview]:
    reviews = {r.name: r for r in cc.review_bids_folder(bids_root) if r.session is not None}
    if unknown := [name for name in names if name not in reviews]:
        raise click.ClickException(f"No such session: {', '.join(unknown)}")
    return [reviews[name] for name in names]


def data_file(bids_root: Path, text: str) -> cc.DataFile:
    """A scan or events file by its name, or its path (below the BIDS folder or whole)."""
    wanted = Path(text)
    places = {wanted.resolve(), (bids_root / wanted).resolve()}
    files = cc.find_data_files(bids_root)
    found = [d for d in files if d.path.resolve() in places or d.path.name == text]
    if len(found) != 1:
        raise click.ClickException(f"Not a scan or events file of {bids_root}: {text}")
    return found[0]


def the_folder(given: Path | None) -> Path:
    return find_folders({"bids": given})[0]["bids"]


def done(action: Callable[[], object], text: str) -> None:
    try:
        action()
    except (cc.CrosscheckError, OSError) as e:
        raise click.ClickException(str(e)) from e
    click.echo(text)


def ask(question: str, yes: bool) -> None:
    if not yes:
        click.echo(question)
        click.confirm("Go ahead?", abort=True)


@main.command()
@folder_option("bids")
@click.argument("sessions", nargs=-1, required=True)
def mark(bids_root: Path | None, sessions: tuple[str, ...]) -> None:
    """Mark SESSIONS as crosschecked."""
    bids = the_folder(bids_root)
    reviews = chosen(bids, sessions)
    done(lambda: cc.set_crosschecked(bids, reviews, True), f"Marked {', '.join(sessions)}")


@main.command()
@folder_option("bids")
@click.argument("sessions", nargs=-1, required=True)
def unmark(bids_root: Path | None, sessions: tuple[str, ...]) -> None:
    """Take the crosschecked mark off SESSIONS."""
    bids = the_folder(bids_root)
    reviews = chosen(bids, sessions)
    text = f"Un-marked {', '.join(sessions)}"
    done(lambda: cc.set_crosschecked(bids, reviews, False), text)


@main.command()
@folder_option("bids")
@click.argument("session")
@click.argument("text")
def note(bids_root: Path | None, session: str, text: str) -> None:
    """Give SESSION a note ('' clears it)."""
    bids = the_folder(bids_root)
    reviews = chosen(bids, (session,))
    done(lambda: cc.set_note(bids, reviews, text), f"Note of {session} saved")


@main.command()
@folder_option("bids")
@click.argument("file")
@yes_option
def keep(bids_root: Path | None, file: str, yes: bool) -> None:
    """Of a scan converted as several runs, keep FILE and delete the others."""
    bids = the_folder(bids_root)
    data = data_file(bids, file)
    same = [
        d
        for d in cc.find_data_files(bids)
        if d.pending and (d.subject, d.session, d.label) == (data.subject, data.session, data.label)
    ]
    if not data.pending:
        raise click.ClickException(f"{file} isn't one of several runs to pick from.")
    others = "\n".join(f"  {d.path.name}" for d in same if d.path != data.path) or "  (none)"
    ask(f"Keep {data.path.name} and delete the other run(s)?\n{others}", yes)
    done(lambda: cc.keep_run(bids, data, same), f"Kept {data.path.name}")


@main.command("delete-file")
@folder_option("bids")
@click.argument("file")
@yes_option
def delete_file(bids_root: Path | None, file: str, yes: bool) -> None:
    """Delete FILE and its sidecar (Convert to BIDS recreates it from the raw data)."""
    bids = the_folder(bids_root)
    data = data_file(bids, file)
    ask(f"Delete {data.path.name} and its sidecar from the BIDS folder?", yes)
    done(lambda: cc.delete_data_file(bids, data), f"Deleted {data.path.name}")


@main.command("delete-session")
@folder_option("bids")
@click.argument("sessions", nargs=-1, required=True)
@yes_option
def delete_session(bids_root: Path | None, sessions: tuple[str, ...], yes: bool) -> None:
    """Delete SESSIONS (dud visits): left out of conversion from now on."""
    bids = the_folder(bids_root)
    reviews = chosen(bids, sessions)
    ask(delete_question(reviews), yes)
    _deleted, errors = delete_sessions(bids, reviews, click.echo)
    if errors:
        raise click.ClickException("\n".join(errors))


@main.command()
@folder_option("bids")
@click.argument("scan")
@click.argument("volumes", type=click.IntRange(0))
def expect(bids_root: Path | None, scan: str, volumes: int) -> None:
    """Set how many VOLUMES the SCAN (e.g. task-Rest_bold) should have (0: automatic)."""
    bids = the_folder(bids_root)
    expected = {k: v for k, v in cc.read_expected(bids).items() if k != scan}
    new = expected | {scan: volumes} if volumes else expected
    done(lambda: cc.write_expected(bids, new), f"{scan}: {volumes or 'automatic'}")


@main.command("sessions-per-subject")
@folder_option("bids")
@click.argument("number", type=click.IntRange(1))
def sessions_per_subject(bids_root: Path | None, number: int) -> None:
    """Set how many sessions each subject should have."""
    bids = the_folder(bids_root)
    text = f"Sessions per subject: {number}"
    done(lambda: cc.write_expected_sessions(bids, number), text)


@main.command()
@folder_option("bids")
@yes_option
def trim(bids_root: Path | None, yes: bool) -> None:
    """Trim every run longer than expected, keeping the first volumes."""
    bids = the_folder(bids_root)
    jobs = cc.long_runs(cc.review_bids_folder(bids))
    if not jobs:
        click.echo("No run is too long.")
        return
    ask(confirm_text(jobs), yes)
    TrimJob(bids, jobs, click.echo).run()


@main.command("backup")
@folder_option("bids")
@click.option("--study", default=backup.DEFAULT_STUDY_ID, show_default=True)
def backup_command(bids_root: Path | None, study: str) -> None:
    """Copy every crosscheck decision to the backup folder outside the BIDS folder."""
    bids = the_folder(bids_root)
    folder = backup.backup_base() / backup.safe_name(study)
    try:
        saved = save_backup(bids, folder)
    except OSError as e:
        raise click.ClickException(str(e)) from e
    click.echo(backup.saved_message(len(saved), folder) if saved else backup.NOTHING_TO_SAVE)


if __name__ == "__main__":
    main()
