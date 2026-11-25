"""Dummy Presentation logs: a real log's shape with none of its content, or one made up whole.

Of a real log the layout is kept (lines, columns, kinds of events, where it stops, the odd
things that trip the reader up); its values are not. Times are redrawn around the task's own
pace, answers, accuracy and reaction times are made up, stimulus names are placeholders.

Presentation's documentation leaves the layout open where it matters here: the delimiter is a
setting (the toolbox reads tabs), and notes about the participant may be put before or after the
tables. So whatever isn't in a table with known columns is masked, not kept.
"""

import datetime as dt
import random
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from fmri_local_process.processing.behav_match import WRITTEN_DATE
from fmri_local_process.processing.dummy_names import MARKERS, Disguise

HEADER_LINE = 3  # 0-based: the first table's column names
EVENT_TYPES = frozenset(
    {"Picture", "Pulse", "Response", "Quit", "Sound", "Pause", "Resume", "Video", "Nothing",
     "Text Input", "Port Input", "Manual"}
)  # fmt: skip
OUTCOMES = ("hit", "incorrect", "miss")
STIM_TYPES = frozenset({*OUTCOMES, "other", "false alarm", "false_alarm"})
COLUMNS = frozenset(
    {"Subject", "Trial", "Event Type", "Code", "Time", "TTime", "Uncertainty", "Duration",
     "ReqTime", "ReqDur", "Stim Type", "Pair Index", "Type", "Response", "RT", "RT Uncertainty",
     "block(str)", "block_num(str)", "block_order(str)", "repeat_num(str)", "stim(str)"}
)  # fmt: skip
KEPT_VALUES = frozenset(
    {"block_fixation", "rating_fixation", "fixation", "thankyou", "match", "shape", "cross",
     "instructions", "rating"}
)  # fmt: skip
CONDITION = re.compile(r"(neg|neu|pos|negative|neutral|positive)[-_](look|decrease|increase|watch)")
PORT_INPUT = re.compile(r"Port Input \d{1,2}")
COUNTERS = frozenset({"Trial", "Pair Index", "block_num(str)", "repeat_num(str)"})
WRITTEN_TIME = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")
NUMBER = re.compile(r"-?\d+(\.\d+)?")
JITTER = 0.2  # of a real log's times: enough that no reaction time can be read off them
MADE_UP_JITTER = 0.03
MAX_RT = 15_000  # 0.1 ms

Cell = Callable[[str, str, dict[str, str]], str]


def written_moment(lines: Sequence[str]) -> tuple[dt.date, dt.time] | None:
    """When the header says the log was written."""
    for line in lines[:HEADER_LINE]:
        found = WRITTEN_DATE.search(line)
        if not found:
            continue
        month, day, year = (int(g) for g in found.groups())
        time = WRITTEN_TIME.search(line[found.end() :])
        try:
            hour, minute, second = (int(g) for g in time.groups()) if time else (0, 0, 0)
            return dt.date(year, month, day), dt.time(hour, minute, second)
        except ValueError:
            return None
    return None


class LogDisguiser:
    def __init__(self, disguise: Disguise) -> None:
        self.disguise, self.rng = disguise, disguise.rng

    def text(self, text: str) -> str:
        """The whole log, disguised."""
        lines = text.splitlines()
        events = [i for i, line in enumerate(lines) if "Event" in line]
        first = HEADER_LINE if events and events[0] == HEADER_LINE else None
        second = events[1] if first is not None and len(events) > 1 else None
        footer = next((i for i, line in enumerate(lines) if "app" in line), len(lines))
        end = second if second is not None else len(lines)
        out = [self.masked(line) for line in lines]
        for i, line in enumerate(lines[:HEADER_LINE]):
            out[i] = self.opening(line, i, written_moment([line]))
        if first is not None:
            out[first] = self.header(lines[first])
            out[first + 1 : end] = self.rows(lines[first], lines[first + 1 : end], timed=True)
        if second is not None:
            stop = footer if footer > second else len(lines)
            out[second] = self.header(lines[second])
            out[second + 1 : stop] = self.rows(lines[second], lines[second + 1 : stop], timed=False)
        return "\n".join(out) + ("\n" if text.endswith(("\n", "\r")) else "")

    def opening(self, line: str, number: int, written: tuple[dt.date, dt.time] | None) -> str:
        if written is not None:
            moment = self.disguise.fake_moment(*written)
            return f"Logfile written - {moment:%m/%d/%Y %H:%M:%S}"
        if number == 0 and line.startswith("Scenario"):
            return "Scenario - dummy"
        return self.masked(line)

    def header(self, line: str) -> str:
        return "\t".join(self.column(name) for name in line.split("\t"))

    def column(self, name: str) -> str:
        if name in COLUMNS or not name.strip() or name.casefold() in self.disguise.keep:
            return name
        return self.disguise.placeholder(name, "extra") + ("(str)" if "(str)" in name else "")

    def masked(self, line: str) -> str:
        """A line outside the tables: as many fields, nothing of what is in them."""
        return "\t".join(self.masked_field(f) for f in line.split("\t"))

    def masked_field(self, value: str) -> str:
        if not value.strip() or value in EVENT_TYPES | COLUMNS | KEPT_VALUES:
            return value  # a table that isn't where it should be still counts its pulses
        if NUMBER.fullmatch(value.strip()):
            return self.disguise.number(re.sub(r"\D", "", value))
        return self.disguise.placeholder(value, "text")

    # ---- tables -------------------------------------------------------------------------------

    def rows(self, header: str, lines: list[str], timed: bool) -> list[str]:
        names = header.split("\t")
        times = self.times(names, lines) if timed else {}
        out = []
        for n, line in enumerate(lines):
            fields = line.split("\t")
            if not line.strip():
                out.append(line)
                continue
            row = {name: value for name, value in zip(names, fields, strict=False)}
            cells = [
                times[n]
                if names[i : i + 1] == ["Time"] and n in times
                else self.cell(names[i] if i < len(names) else "", value, row)
                for i, value in enumerate(fields)
            ]
            out.append("\t".join(cells))
        return out

    def times(self, names: list[str], lines: list[str]) -> dict[int, str]:
        """New times per line: the steps between events redrawn, answers put anywhere before the
        next event."""
        if "Time" not in names:
            return {}
        at = names.index("Time")
        kind_at = names.index("Event Type") if "Event Type" in names else -1
        real: list[tuple[int, int, bool]] = []
        for n, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) > at and NUMBER.fullmatch(fields[at].strip()):
                answer = 0 <= kind_at < len(fields) and fields[kind_at] == "Response"
                real.append((n, int(float(fields[at])), answer))
        out: dict[int, str] = {}
        fake, before = self.rng.randint(1_000, 50_000), None
        steady = [(n, time) for n, time, answer in real if not answer]
        new_times = {}
        for n, time in steady:
            if before is not None:
                fake += max(1, round((time - before) * self.rng.uniform(1 - JITTER, 1 + JITTER)))
            new_times[n], before = fake, time
        for n, _time, answer in real:
            if answer:
                earlier = [t for m, t in new_times.items() if m < n]
                later = [t for m, t in new_times.items() if m > n]
                low = max(earlier, default=fake)
                high = min(later, default=low + MAX_RT)
                out[n] = str(self.rng.randint(low, max(low, high)))
            else:
                out[n] = str(new_times[n])
        return {n: self.not_secret(value) for n, value in out.items()}

    def not_secret(self, value: str) -> str:
        """A time that happens to look like a real ID becomes another one."""
        while value in self.disguise.secrets:
            value = str(int(value) + 1)
        return value

    def cell(self, name: str, value: str, row: dict[str, str]) -> str:
        stripped = value.strip()
        if not stripped:
            return value
        match name:
            case "Subject":
                return self.disguise.fake_id(stripped)
            case "Event Type":
                known = stripped in EVENT_TYPES or PORT_INPUT.fullmatch(stripped)
                return stripped if known else "Other"
            case "Code" if row.get("Event Type") == "Response":
                return str(self.rng.randint(1, 4))
            case "Stim Type" | "Type":
                return self.outcome(stripped)
            case "Response":
                return str(self.rng.randint(1, 4)) if NUMBER.fullmatch(stripped) else "other"
            case "RT":
                return str(self.rng.randint(2_000, MAX_RT)) if stripped != "0" else "0"
        if name in COUNTERS and re.fullmatch(r"\d{1,4}[A-Z]?", stripped):  # 3A: a feedback trial
            return stripped
        if NUMBER.fullmatch(stripped):
            return self.noisy(stripped)
        lower = stripped.casefold()
        if lower in KEPT_VALUES or lower in self.disguise.keep or CONDITION.fullmatch(lower):
            return stripped
        return self.disguise.placeholder(stripped, "stim")

    def outcome(self, value: str) -> str:
        if value in OUTCOMES:
            return self.rng.choices(OUTCOMES, (8, 1, 1))[0]
        return value if value in STIM_TYPES else "other"

    def noisy(self, value: str) -> str:
        number = float(value)
        if number == 0:
            return value
        if abs(number) < 10:  # may be an answer
            return str(self.rng.randint(1, 9))
        redrawn = number * self.rng.uniform(1 - JITTER, 1 + JITTER)
        fake = str(round(redrawn) + self.rng.randint(0, 9))
        while fake in self.disguise.secrets or fake == value:
            fake = str(int(fake) + 1)
        return fake


# ---- made-up logs -------------------------------------------------------------------------------

BASE_COLUMNS = [
    "Subject", "Trial", "Event Type", "Code", "Time", "TTime", "Uncertainty", "Duration",
    "Uncertainty", "ReqTime", "ReqDur", "Stim Type", "Pair Index",
]  # fmt: skip
TASK_COLUMNS = {
    "emolabeling": (["block(str)", "block_num(str)", "block_order(str)"], 21),
    "cogreappraisal": (["block(str)", "block_num(str)", "repeat_num(str)", "stim(str)"], 18),
}
RESPONSE_COLUMNS = ["Event Type", "Code", "Type", "Response", "RT"]
RESPONSE_WIDTH = 12
SECOND = 10_000  # Presentation's 0.1 ms
BLOCKS = {
    "emolabeling": ["match", "shape"],
    "cogreappraisal": ["neg-look", "neg-decrease", "neu-look"],
}
N_BLOCKS = 12
FIXATION_S, BLOCK_S, THANKYOU_S = 8.0, 20.0, 4.0
PULSE_S = 0.832


@dataclass
class MadeUpLog:
    """One whole log, invented."""

    task: str  # a TASK_COLUMNS key
    subject: str
    written: dt.datetime
    complete: bool = True
    extra_columns: int = 0  # a layout the R script doesn't expect
    rng: random.Random = field(default_factory=random.Random)

    def text(self) -> str:
        own, width = TASK_COLUMNS[self.task]
        columns = BASE_COLUMNS + own
        columns += [f"extra{n}(str)" for n in range(width + self.extra_columns - len(columns))]
        events, answers = self.events()
        lines = ["Scenario - dummy", f"Logfile written - {self.written:%m/%d/%Y %H:%M:%S}", ""]
        lines += ["\t".join(columns), ""]
        for trial, event in enumerate(events, start=1):
            row = {"Subject": self.subject, "Trial": str(trial), **event}
            lines.append("\t".join(row.get(c, "") for c in columns).rstrip("\t"))
        names = [*RESPONSE_COLUMNS, *(f"extra{n}" for n in range(RESPONSE_WIDTH - 5))]
        lines += ["", "", "\t".join(names), ""]
        lines += ["\t".join(a.get(c, "") for c in names).rstrip("\t") for a in answers]
        lines += ["", "", "", "dummy app summary"]
        return "\n".join(lines) + "\n"

    def events(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        emo = self.task == "emolabeling"
        where = "block_order(str)" if emo else "repeat_num(str)"
        events: list[dict[str, str]] = []
        answers: list[dict[str, str]] = []
        now = self.rng.randint(20_000, 60_000)

        def add(seconds: float, **cells: str) -> None:
            nonlocal now
            length = round(seconds * SECOND)
            row = {"Event Type": "Picture", "Time": str(now), "Duration": str(length)}
            events.append(row | cells)
            now += round(length * self.rng.uniform(1 - MADE_UP_JITTER, 1 + MADE_UP_JITTER))

        start = now
        last = N_BLOCKS if self.complete else N_BLOCKS // 2
        for block in range(1, last + 1):
            fixation = {where: "block_fixation", "Code": f"fix{block}"}
            add(FIXATION_S, **(fixation if emo else fixation | {"stim(str)": "cross"}))
            kind = BLOCKS[self.task][(block - 1) % len(BLOCKS[self.task])]
            cells = {"block(str)": kind, "block_num(str)": str(block), "Code": f"stim{block:02d}"}
            if emo:
                add(BLOCK_S, **cells)
                outcome = self.rng.choices(OUTCOMES, (8, 1, 1))[0]
                answers.append({"Event Type": "Picture", "Code": cells["Code"], "Type": outcome})
            else:
                add(BLOCK_S, **cells, **{"repeat_num(str)": "1"})
                rating = {"repeat_num(str)": "1", "stim(str)": "rating_fixation"}
                add(FIXATION_S / 2, **cells, **rating)
                events.append(
                    {"Event Type": "Response", "Code": str(self.rng.randint(1, 4)),
                     "Time": str(now - SECOND)}
                )  # fmt: skip
        if self.complete:
            add(FIXATION_S, **{where: "block_fixation", "Code": "fix_end"})
            add(THANKYOU_S, **{where: "thankyou", "Code": "end"})
        else:
            events.append({"Event Type": "Quit", "Time": str(now)})
        pulses = [
            {"Event Type": "Pulse", "Code": "255", "Time": str(t)}
            for t in range(start - 2_000, now, round(PULSE_S * SECOND))
        ]
        return sorted(pulses + events, key=lambda e: int(e["Time"])), answers


def keeps_markers(real: str, dummy: str) -> bool:
    """The log reader finds its way by these: a dummy has them on the same lines."""
    pairs = zip(real.splitlines(), dummy.splitlines(), strict=True)
    return all((m in r) == (m in d) for r, d in pairs for m in MARKERS)


def vocabulary() -> set[str]:
    """Words any log has anyway, whoever is called that: not searched for as leaks."""
    texts = [*COLUMNS, *EVENT_TYPES, *STIM_TYPES, *KEPT_VALUES, *BASE_COLUMNS]
    texts += ["Scenario - dummy", "Logfile written", "extra stim text word", "dummy app summary"]
    texts += ["negative neutral positive look decrease increase watch"]
    return set(re.findall(r"[a-z]+", " ".join(texts).casefold()))
