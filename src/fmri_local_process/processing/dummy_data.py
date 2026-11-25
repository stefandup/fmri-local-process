"""Dummy raw data (DICOM session folders and behavioural logs) to try the toolbox on, modelled
on the dummy-data generators of vr_lab_toolbox.

Two sources, either or both:
* a real study's folders: each log and session folder gets a dummy twin with the same shape, so
  that IDs and dates match, or don't, exactly where they do in the real data. Nothing else is
  passed on (dummy_names.py, dummy_logs.py, dummy_dicom.py), and the link between real and dummy
  IDs is never written down.
* made-up scenarios: a study invented whole, one participant per thing that can go wrong.

At the end the output is searched for everything that should have been replaced. If any of it is
found, the output is deleted and nothing is left behind.
"""

import datetime as dt
import logging
import random
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from fmri_local_process.processing import behav_match as bm
from fmri_local_process.processing.behav_logs import find_logs, raw_log_id, read_log_lines
from fmri_local_process.processing.crosscheck import STUDY_VOLUMES
from fmri_local_process.processing.dummy_dicom import SeriesShape, read_shape, write_series
from fmri_local_process.processing.dummy_logs import (
    LogDisguiser,
    MadeUpLog,
    vocabulary,
    written_moment,
)
from fmri_local_process.processing.dummy_names import (
    LOG_WORDS,
    SERIES_WORDS,
    TASK_WORDS,
    Disguise,
)
from fmri_local_process.processing.philani_bids import SCAN_RULES

log = logging.getLogger(__name__)

BEHAV_DIR, DICOM_DIR = "behav", "dicom"
DUMMY_DATA_LOG_FILENAME = "dummy_data_log.txt"
NO_WORDS: frozenset[str] = frozenset()
SERIES_DIGITS = 3
BOLD_RULES = tuple(rule for rule in SCAN_RULES if rule.suffix == "bold")
Progress = Callable[[str, int, int], None]  # what, done, total


class DummyDataError(Exception):
    """The dummy data can't be made; the message says why."""


class LeakError(DummyDataError):
    """Something real was found in the output, which has been deleted."""


@dataclass(frozen=True)
class DummyParticipantResult:
    """One made-up participant."""

    subject_id: str
    scenario: str
    log_ids: tuple[str, ...]  # the IDs their logs say
    sessions: int
    logs: int


@dataclass
class DummyReport:
    sessions: int = 0
    series: int = 0
    images: int = 0
    logs: int = 0
    unlike: int = 0  # dummy logs the toolbox reads differently from their originals
    no_tabs: int = 0  # logs without a tab: another delimiter, or not a Presentation log
    unreadable: int = 0
    placeholders: int = 0
    reference_sessions: int = 0  # of the above, the dummy twins of real ones
    reference_logs: int = 0
    participants: list[DummyParticipantResult] = field(default_factory=list)


# ---- from a real study --------------------------------------------------------------------------


def session_name_parts(name: str) -> tuple[str, str, str, str] | None:
    """(date, time, ID, rest) of <YYYYMMDD>_<time>_<ID>[_...], as the converter splits it."""
    parts = name.split("_", 3)
    if len(parts) < 3 or parse_day(parts[0]) is None:
        return None
    return parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else ""


def parse_day(text: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def folder_time(text: str) -> dt.time | None:
    try:
        return dt.datetime.strptime(text, "%H%M").time() if len(text) == 4 else None
    except ValueError:
        return None


def id_in_name(path: Path) -> str:
    """The ID as written at the start of a log's name (without '.log', if that is all)."""
    raw = raw_log_id(path)
    return path.stem if raw == path.name else raw


class StudyCopy:
    """A real study's dummy twin."""

    def __init__(
        self,
        behav_root: Path | None,
        dicom_root: Path | None,
        rng: random.Random,
        progress: Progress | None = None,
    ) -> None:
        self.behav_root, self.dicom_root, self.rng = behav_root, dicom_root, rng
        self.disguise = Disguise(rng)
        self.progress = progress or (lambda _what, _done, _total: None)
        self.logs = list(find_logs(behav_root)) if behav_root else []
        self.folders = sorted(d for d in dicom_root.iterdir() if d.is_dir()) if dicom_root else []
        self.texts: dict[Path, str] = {}
        self.learn()

    def learn(self) -> None:
        """Every ID, date, time and name, before anything is written."""
        disguise = self.disguise
        for folder in self.folders:
            parts = session_name_parts(folder.name)
            if parts is None:
                disguise.secret_words(folder.name, SERIES_WORDS)
                continue
            day = parse_day(parts[0])
            assert day is not None
            disguise.learn_id(parts[2])
            disguise.learn_moment(day, folder_time(parts[1]))
            disguise.secret_words(parts[3], NO_WORDS)
            disguise.secret_words(parts[2], NO_WORDS)
        for n, path in enumerate(self.logs, start=1):
            self.progress("Reading logs", n, len(self.logs))
            self.learn_log(path)
        disguise.settle()

    def learn_log(self, path: Path) -> None:
        assert self.behav_root is not None
        disguise = self.disguise
        raw = id_in_name(path)
        disguise.learn_id(raw)
        disguise.secret_words(raw, NO_WORDS)
        for part in path.relative_to(self.behav_root).parts:
            disguise.secret_words(part, LOG_WORDS)
            if day := parse_day(part):
                disguise.learn_moment(day)
        stamp = bm.filename_time(path.name)
        if stamp is not None:
            disguise.learn_moment(stamp.date(), stamp.time())
        elif day := bm.filename_date(path.name):
            disguise.learn_moment(day)
        try:
            lines = read_log_lines(path)
        except OSError:
            return
        self.texts[path] = "\n".join(lines) + "\n"
        if written := written_moment(lines):
            disguise.learn_moment(*written)
        for line in lines[4:]:
            subject = line.split("\t", 1)[0].strip()
            if "\t" in line and 2 < len(subject) <= 20 and subject != "Event Type":
                if lines[3:4] and lines[3].startswith("Subject\t") and subject != "Picture":
                    disguise.learn_id(subject)
                break

    # ---- writing ------------------------------------------------------------------------------

    def write(self, out: Path, images: bool, report: DummyReport) -> None:
        for n, folder in enumerate(self.folders, start=1):
            self.progress("Making DICOM sessions", n, len(self.folders))
            self.write_session(folder, out / DICOM_DIR, images, report)
        for n, path in enumerate(self.logs, start=1):
            self.progress("Making logs", n, len(self.logs))
            self.write_log(path, out / BEHAV_DIR, report)
        report.placeholders = len(self.disguise.words)
        report.reference_sessions, report.reference_logs = report.sessions, report.logs

    def session_name(self, name: str) -> str:
        parts = session_name_parts(name)
        if parts is None:
            return self.disguise.text(name, SERIES_WORDS, SERIES_DIGITS)
        day, time_text, raw_id, rest = parts
        real_day, time = parse_day(day), folder_time(time_text)
        assert real_day is not None
        if time is not None:
            fake = self.disguise.fake_moment(real_day, time)
            start = f"{fake:%Y%m%d}_{fake:%H%M}"
        else:
            start = f"{self.disguise.fake_day(real_day):%Y%m%d}_"
            start += self.disguise.text(time_text, NO_WORDS)
        tail = f"_{self.disguise.text(rest, NO_WORDS)}" if "_" in name[len(day) :] and rest else ""
        return f"{start}_{self.disguise.fake_id(raw_id)}{tail}"

    def write_session(self, folder: Path, root: Path, images: bool, report: DummyReport) -> None:
        target = unused(root / self.session_name(folder.name))
        parts = session_name_parts(folder.name)
        subject = self.disguise.fake_id(parts[2]) if parts else "DUMMY"
        target.mkdir(parents=True)
        report.sessions += 1
        listings: dict[tuple[str, ...], int] = {}  # copies of a series keep equal file names
        for real in [folder, *sorted(d for d in folder.rglob("*") if d.is_dir())]:
            names = tuple(sorted(f.name for f in real.iterdir() if f.is_file()))
            if not names:
                continue
            relative = [
                self.disguise.text(part, SERIES_WORDS, SERIES_DIGITS)
                for part in real.relative_to(folder).parts
            ]
            copy = listings.setdefault(names, len(listings) + 1)
            bold = any(fnmatchcase(real.as_posix(), rule.pattern) for rule in BOLD_RULES)
            matched = any(fnmatchcase(real.as_posix(), rule.pattern) for rule in SCAN_RULES)
            full = images and matched
            shape = read_shape(real, bold) if full else SeriesShape()
            number = re.match(r"\d{1,4}", real.name)
            report.series += 1
            report.images += write_series(
                target.joinpath(*relative),
                shape,
                subject,
                int(number.group()) if number else copy,
                self.rng,
                prefix=f"IM{copy:03d}_",
                images=full,
            )

    def write_log(self, path: Path, root: Path, report: DummyReport) -> None:
        assert self.behav_root is not None
        target = unused(root / self.log_path(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        report.logs += 1
        text = self.texts.get(path)
        if text is None:  # can't be read: nor can its dummy
            report.unreadable += 1
            target.write_bytes(b"")
            return
        report.no_tabs += "\t" not in text
        target.write_text(LogDisguiser(self.disguise).text(text), encoding="utf-8")
        real = bm.inspect_log(path, self.behav_root)
        report.unlike += likeness(real) != likeness(bm.inspect_log(target, root))

    def log_path(self, path: Path) -> Path:
        assert self.behav_root is not None
        *folders, name = path.relative_to(self.behav_root).parts
        raw = id_in_name(path)
        rest = name[len(raw) :] if name.startswith(raw) else name
        stem, dot, ending = rest.rpartition(".")
        fake = self.disguise.fake_id(raw) + self.disguise.text(stem, LOG_WORDS) + dot + ending
        return Path(*(self.disguise.text(f, LOG_WORDS) for f in folders), fake)


def likeness(info: bm.LogInfo) -> tuple:
    """What a dummy log must share with its original: how the toolbox reads it."""
    return (
        info.task,
        info.state,
        info.date_source,
        len(info.columns),
        info.pulses,
        sorted(info.trial_types.values()),
        len(info.notes),
    )


def unused(path: Path) -> Path:
    """path, or path with a number when two names became the same one."""
    n, candidate = 2, path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return candidate


def check_for_leaks(out: Path, secrets: Iterable[str]) -> int:
    """How many places in the output hold something that should have been replaced, as a whole
    word: in every name, and in the logs (DICOM files are written from scratch).

    A log is full of numbers, so an all-digit ID is only looked for where an ID can be: in the
    Subject column and outside the tables."""
    known = vocabulary() | SERIES_WORDS | LOG_WORDS | set(TASK_WORDS)
    words = {s.casefold() for s in secrets if len(s) >= 4} - known
    numbers = {w for w in words if w.isdigit()}
    found = 0
    for path in out.rglob("*"):
        found += holds(path.name, words)
        if path.is_file() and path.suffix != ".dcm":
            text = path.read_text(encoding="utf-8", errors="replace")
            found += holds(text, words - numbers)
            outside = [line.split("\t", 1)[0] for line in text.splitlines()]
            found += holds("\n".join(outside), numbers)
    return found


def holds(text: str, words: set[str]) -> int:
    tokens = set(re.findall(r"[a-z0-9]+", text.casefold()))
    letters = set(re.findall(r"[a-z]{4,}", text.casefold()))
    return len((tokens | letters) & words)


# ---- made-up scenarios --------------------------------------------------------------------------

LABELS = {"emolabeling": "labeling_task_AP", "cogreappraisal": "Cog_reappraisal_task_AP"}
BOLD_OF = {"emolabeling": "task-Matching_bold", "cogreappraisal": "task-Reappraisal_bold"}
TASKS = tuple(LABELS)
FIRST_DAY = dt.date(2002, 3, 4)


CLEAN_SCENARIO_LABEL = "clean"


@dataclass(frozen=True)
class Scenario:
    about: str
    expected: tuple[str, ...]  # match status the toolbox gives the emolabeling log(s)


SCENARIOS = {
    CLEAN_SCENARIO_LABEL: Scenario("Everything there and right.", (bm.MATCHED,)),
    "twins": Scenario("Two twins, <ID>-1 and <ID>-2, scanned the same day.", (bm.MATCHED,) * 2),
    "two_sessions": Scenario("Two visits, with logs for each.", (bm.MATCHED,) * 2),
    "log_id_typo": Scenario("The logs' ID has a digit too many: correct it.", (bm.UNKNOWN_ID,)),
    "log_id_space": Scenario("A space in the logs' ID: removed and flagged.", (bm.MATCHED,)),
    "wrong_log_date": Scenario("Two visits; the logs are dated on neither.", (bm.DATE_MISMATCH,)),
    "duplicate_logs": Scenario("The task was restarted: pick a log.", (bm.DUPLICATE,) * 2),
    "aborted_log": Scenario("The logs stop half way: they need a verdict.", (bm.MATCHED,)),
    "unreadable_log": Scenario("Not Presentation logs inside.", (bm.MATCHED,)),
    "missing_log": Scenario("Scanned, but no logs: listed as missing.", ()),
    "missing_scan": Scenario("Logs of someone who was never scanned.", (bm.UNKNOWN_ID,)),
    "missing_task_scan": Scenario("Scanned that day, but not these tasks.", (bm.NO_SCAN,)),
    "no_task_in_log_path": Scenario("The log's path doesn't say which task.", ()),
    "log_date_in_header_only": Scenario("No date in the logs' names.", (bm.MATCHED,)),
    "odd_log_columns": Scenario("A layout the R script doesn't expect.", (bm.MATCHED,)),
    "dicom_folder_without_date": Scenario("The DICOM folder has no date.", (bm.UNKNOWN_ID,)),
    "copied_series": Scenario("A second copy of a series in Untitled/.", (bm.MATCHED,)),
    "repeated_scan": Scenario("The task was scanned twice: keep one.", (bm.MATCHED,)),
}
ERROR_TYPES = tuple(name for name in SCENARIOS if name != CLEAN_SCENARIO_LABEL)


class MadeUpStudy:
    def __init__(self, out: Path, rng: random.Random, images: bool, report: DummyReport) -> None:
        self.out, self.rng, self.images, self.report = out, rng, images, report

    def write(self, n_clean: int, with_errors: bool, progress: Progress | None = None) -> None:
        scenarios = [CLEAN_SCENARIO_LABEL] * n_clean + list(ERROR_TYPES if with_errors else ())
        for index, scenario in enumerate(scenarios):
            if progress is not None:
                progress("Making participants", index + 1, len(scenarios))
            subject = f"DUMMY{index:03d}"
            sessions, logs = self.report.sessions, self.report.logs
            day = FIRST_DAY + dt.timedelta(days=7 * (index + 1))
            log_ids = self.participant(scenario, subject, dt.datetime.combine(day, dt.time(9)))
            log.info("Made %s (%s)", subject, scenario)
            self.report.participants.append(
                DummyParticipantResult(
                    subject,
                    scenario,
                    log_ids,
                    self.report.sessions - sessions,
                    self.report.logs - logs,
                )
            )

    def participant(self, scenario: str, subject: str, when: dt.datetime) -> tuple[str, ...]:
        """Their folders and logs; the IDs the logs say."""
        later = when + dt.timedelta(days=90)
        log_ids: tuple[str, ...] = (subject,)
        match scenario:
            case "twins":
                log_ids = (f"{subject}-1", f"{subject}-2")
                for hours, twin in enumerate(log_ids):
                    start = when + dt.timedelta(hours=hours)
                    self.session(twin, start)
                    self.logs(twin, start)
            case "two_sessions":
                for start in (when, later):
                    self.session(subject, start)
                    self.logs(subject, start)
            case "log_id_typo":
                self.session(subject, when)
                log_ids = (subject + subject[-1],)
                self.logs(log_ids[0], when)
            case "log_id_space":
                self.session(subject, when)
                self.logs(f"{subject[:5]} {subject[5:]}", when)
            case "wrong_log_date":
                self.session(subject, when)
                self.session(subject, later)
                self.logs(subject, when + dt.timedelta(days=30))
            case "duplicate_logs":
                self.session(subject, when)
                self.logs(subject, when)
                self.logs(subject, when + dt.timedelta(minutes=10), tasks=("emolabeling",))
            case "aborted_log":
                self.session(subject, when)
                self.logs(subject, when, complete=False)
            case "unreadable_log":
                self.session(subject, when)
                for path in self.logs(subject, when):
                    path.write_text("this is not a Presentation log\n", encoding="utf-8")
            case "missing_log":
                self.session(subject, when)
            case "missing_scan":
                self.logs(subject, when)
            case "missing_task_scan":
                self.session(subject, when, tasks=())
                self.logs(subject, when)
            case "no_task_in_log_path":
                self.session(subject, when)
                self.log_file("other", f"{subject}-sometask", "emolabeling", subject, when)
            case "log_date_in_header_only":
                self.session(subject, when)
                for task in TASKS:
                    path = self.out / BEHAV_DIR / task / f"{subject}-{task}.log"
                    self.write_log(path, MadeUpLog(task, subject, when, rng=self.rng))
            case "odd_log_columns":
                self.session(subject, when)
                self.logs(subject, when, extra_columns=2)
            case "dicom_folder_without_date":
                self.session(subject, when, name=f"visit one {subject}")
                self.logs(subject, when)
            case "copied_series":
                folder = self.session(subject, when)
                for series in [d for d in sorted(folder.iterdir()) if d.name.endswith("_rest_AP")]:
                    shutil.copytree(series, folder / "Untitled" / series.name)
                self.logs(subject, when)
            case "repeated_scan":
                folder = self.session(subject, when)
                self.series(folder, 9, "emolabeling", subject)
                self.logs(subject, when)
            case _:
                self.session(subject, when)
                self.logs(subject, when)
        return log_ids

    def session(
        self,
        subject: str,
        when: dt.datetime,
        tasks: tuple[str, ...] = TASKS,
        name: str = "",
    ) -> Path:
        folder = self.out / DICOM_DIR / (name or f"{when:%Y%m%d_%H%M}_{subject}")
        self.report.sessions += 1
        self.plain_series(folder, 2, "ABCD_T1w_MPR_vNav_RMS", SeriesShape(), subject)
        rest = SeriesShape(STUDY_VOLUMES["task-Rest_bold"])
        self.plain_series(folder, 3, "cmrr_tfMRI_rest_AP", rest, subject)
        for number, task in enumerate(tasks, start=4):
            self.series(folder, number, task, subject)
        return folder

    def series(self, folder: Path, number: int, task: str, subject: str) -> None:
        shape = SeriesShape(STUDY_VOLUMES[BOLD_OF[task]], planned=STUDY_VOLUMES[BOLD_OF[task]])
        self.plain_series(folder, number, f"cmrr_tfMRI_{LABELS[task]}", shape, subject)

    def plain_series(
        self, folder: Path, number: int, name: str, shape: SeriesShape, subject: str
    ) -> None:
        self.report.series += 1
        self.report.images += write_series(
            folder / f"{number}_{name}", shape, subject, number, self.rng, images=self.images
        )

    def logs(
        self,
        subject: str,
        when: dt.datetime,
        tasks: tuple[str, ...] = TASKS,
        complete: bool = True,
        extra_columns: int = 0,
    ) -> list[Path]:
        paths = []
        for n, task in enumerate(tasks):
            start = when + dt.timedelta(minutes=20 + 10 * n)
            stem = f"{subject}-{task}_scan-{start:%Y%m%d-%H%M%S}-{self.code()}"
            path = self.log_file(task, stem, task, subject, start, complete, extra_columns)
            paths.append(path)
        return paths

    def log_file(
        self,
        folder: str,
        stem: str,
        task: str,
        subject: str,
        when: dt.datetime,
        complete: bool = True,
        extra_columns: int = 0,
    ) -> Path:
        path = self.out / BEHAV_DIR / folder / f"{when:%Y}" / f"{stem}.log"
        made_up = MadeUpLog(task, subject, when, complete, extra_columns, self.rng)
        return self.write_log(path, made_up)

    def write_log(self, path: Path, made_up: MadeUpLog) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(made_up.text(), encoding="utf-8")
        self.report.logs += 1
        return path

    def code(self) -> str:
        return "".join(self.rng.choice("BCDFGHJK") for _ in range(5))


# ---- all of it ----------------------------------------------------------------------------------


def generate_dummy_dataset(
    output_folder: Path,
    n_clean: int = 5,
    with_errors: bool = False,
    reference_behav: Path | None = None,
    reference_dicom: Path | None = None,
    images: bool = False,
    seed: int | None = None,
    progress: Progress | None = None,
) -> DummyReport:
    """Dummy data in output_folder (new or empty): n_clean well-formed participants, with_errors
    one per error scenario, and a dummy twin of the reference folders, if given."""
    for root in (reference_behav, reference_dicom):
        if root is not None:
            check_apart(output_folder, root)
    if output_folder.exists() and any(output_folder.iterdir()):
        raise DummyDataError(f"{output_folder} isn't empty: give a new folder.")
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    report = DummyReport()
    copy = None
    if reference_behav is not None or reference_dicom is not None:
        copy = StudyCopy(reference_behav, reference_dicom, rng, progress)
        if not copy.logs and not copy.folders:
            raise DummyDataError("No .log files and no session folders found there.")
    elif not n_clean and not with_errors:
        raise DummyDataError("Nothing to make: no participants and no reference folders.")
    try:
        if copy is not None:
            copy.write(output_folder, images, report)
            if leaks := check_for_leaks(output_folder, copy.disguise.secrets):
                raise LeakError(
                    f"{leaks} name(s) or ID(s) of the real data were found in the dummy data, "
                    "so it was deleted. Please report this; nothing was left behind."
                )
        MadeUpStudy(output_folder, rng, images, report).write(n_clean, with_errors, progress)
        write_dummy_data_log(report, output_folder, images)
    except BaseException:
        if output_folder.exists():
            shutil.rmtree(output_folder)
        raise
    return report


def write_dummy_data_log(report: DummyReport, output_folder: Path, images: bool) -> Path:
    """What each participant is for, one line each."""
    lines = ["Dummy data made by fmri_generate_sample_data. No real participant is in here."]
    if not images:
        lines.append("Each series is one small image: for matching, not for converting.")
    if report.reference_sessions or report.reference_logs:
        lines.append(
            f"Dummy twins of a real study: {report.reference_sessions} session folder(s), "
            f"{report.reference_logs} log(s), matching or not where the real ones do, with made-up "
            "IDs, dates, times, answers and images."
        )
    lines += [
        f"{r.subject_id} [{r.scenario}]: {SCENARIOS[r.scenario].about} "
        f"(sessions={r.sessions}, logs={r.logs})"
        for r in report.participants
    ]
    path = output_folder / DUMMY_DATA_LOG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def check_apart(out: Path, root: Path) -> None:
    a, b = out.resolve(), root.resolve()
    if a == b or a in b.parents or b in a.parents:
        raise DummyDataError(f"The dummy data can't go in (or around) the real data: {root}")
