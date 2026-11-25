"""fmri_behav_fix: record what the crosscheck of logs and DICOM folders found, as the Raw DICOMs
and Behaviour logs tabs do. Everything is saved in the BIDS folder (code/) and applied by Convert to
BIDS; the raw files are never touched."""

from dataclasses import replace
from pathlib import Path

import click

from fmri_local_process.app.behav import (
    BehavWork,
    load_behav_data,
    parse_date,
    parse_session,
)
from fmri_local_process.cli.behav_check import one_log
from fmri_local_process.cli.folders import find_folders, folder_option
from fmri_local_process.processing import philani_bids as pb
from fmri_local_process.processing.behav_logs import find_logs
from fmri_local_process.processing.behav_match import (
    REVIEW_ERROR,
    REVIEW_NOT_USED,
    REVIEW_OK,
    Correction,
    correction_text,
    read_corrections,
    same_day_rivals,
    write_corrections,
)

VERDICTS = {"ok": REVIEW_OK, "error": REVIEW_ERROR, "not-used": REVIEW_NOT_USED, "none": None}
EPILOG = """
Examples, for the folders set with fmri_folders (a part of a log's path is enough):

\b
  fmri_behav_fix log P0O1-emo --id P001
  fmri_behav_fix log P001-cog --verdict error --note "wrong task"
  fmri_behav_fix folder 20240403_0930_P0O1 --id P001 --session 2
  fmri_behav_fix use P001-emolabeling_scan-20240403-100000
"""


@click.group(epilog=EPILOG)
@click.version_option(package_name="fmri-local-process")
def main() -> None:
    """Record corrections and verdicts on logs and DICOM folders."""


@main.command()
@click.argument("log", type=str)
@folder_option("bids")
@folder_option("behav")
@click.option("--id", "subject", help="The right subject ID ('' to take the correction out).")
@click.option("--date", help="The right date, YYYY-MM-DD ('' to take the correction out).")
@click.option("--verdict", type=click.Choice(list(VERDICTS)))
@click.option("--note", help="Why ('' to clear it).")
@click.option("--crosschecked/--not-crosschecked", default=None, help="You have been through it.")
def log(
    log: str,
    bids_root: Path | None,
    behav_root: Path | None,
    subject: str | None,
    date: str | None,
    verdict: str | None,
    note: str | None,
    crosschecked: bool | None,
) -> None:
    """Correct LOG's ID or date, give a verdict, or mark it as crosschecked."""
    roots, _more, _remembered = find_folders({"bids": bids_root, "behav": behav_root})
    logs = find_logs(roots["behav"])
    key = one_log(log, [p.relative_to(roots["behav"]).as_posix() for p in logs])
    corrections = read_corrections(roots["bids"])
    new = corrections.get(key, Correction())
    try:
        if subject is not None:
            new = replace(new, subject=subject.strip() or None)
        if date is not None:
            new = replace(new, date=parse_date(date))
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    if verdict is not None:
        review = VERDICTS[verdict]
        new = replace(new, review=review, note=new.note if review else "")
    if note is not None:
        new = replace(new, note=note.strip())
    if crosschecked is not None:
        new = replace(new, crosschecked=crosschecked)
    if new.note and not new.review:
        raise click.ClickException("A note goes with a verdict: give --verdict too.")
    corrections.pop(key, None)
    if new != Correction():
        corrections[key] = new
    write_corrections(roots["bids"], corrections)
    click.echo(f"{key}: {correction_text(new)}")


@main.command()
@click.argument("name")
@folder_option("bids")
@click.option("--id", "subject", help="The right subject ID ('' to take the correction out).")
@click.option("--date", help="The real scan date, YYYY-MM-DD ('' to take it out).")
@click.option("--session", help="Its BIDS session number ('' for: numbered by date).")
@click.option("--crosschecked/--not-crosschecked", default=None, help="You have been through it.")
def folder(
    name: str,
    bids_root: Path | None,
    subject: str | None,
    date: str | None,
    session: str | None,
    crosschecked: bool | None,
) -> None:
    """Correct the DICOM session folder NAME's ID, date or session number."""
    try:
        day = parse_date(date) if date is not None else None
        label = parse_session(session) if session is not None else None
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    bids = find_folders({"bids": bids_root})[0]["bids"]
    if subject is not None:
        pb.write_dicom_corrections(
            bids, changed(pb.read_dicom_corrections(bids), name, subject.strip())
        )
    if date is not None:
        pb.write_dicom_date_corrections(
            bids, changed(pb.read_dicom_date_corrections(bids), name, day)
        )
    if session is not None:
        pb.write_dicom_session_corrections(
            bids, changed(pb.read_dicom_session_corrections(bids), name, label)
        )
    if crosschecked is not None:
        folders = pb.read_crosschecked_folders(bids)
        pb.write_crosschecked_folders(bids, folders | {name} if crosschecked else folders - {name})
    click.echo(f"{name}: saved. Check with fmri_behav_check.")


def changed[T](values: dict[str, T], key: str, value: T | None) -> dict[str, T]:
    rest = {k: v for k, v in values.items() if k != key}
    return rest | {key: value} if value else rest


@main.command()
@click.argument("log", type=str)
@folder_option("bids")
@folder_option("behav")
@folder_option("dicom")
@click.option("--note", default="", help="Why this one.")
def use(
    log: str,
    bids_root: Path | None,
    behav_root: Path | None,
    dicom_root: Path | None,
    note: str,
) -> None:
    """Of several logs of one day, use LOG: the others are marked 'not used'."""
    roots, _more, _remembered = find_folders(
        {"bids": bids_root, "behav": behav_root, "dicom": dicom_root}
    )
    folders = (roots["bids"], roots["behav"], roots["dicom"])
    work = BehavWork(*folders, load_behav_data(*folders))
    key = one_log(log, [m.info.key for m in work.matches])
    match = next(m for m in work.matches if m.info.key == key)
    rivals = same_day_rivals(work.matches, match)
    if not rivals:
        raise click.ClickException("There is no other log of that subject, task and day.")
    work.use_this_one(match, note)
    typed = {i.key: (i.correction.subject or "", i.correction.date) for i in work.infos}
    work.set_corrections(typed)
    work.save()
    click.echo(f"{match.info.key}: used")
    for rival in rivals:
        click.echo(f"{rival.info.key}: {rival.info.correction.review}")


if __name__ == "__main__":
    main()
