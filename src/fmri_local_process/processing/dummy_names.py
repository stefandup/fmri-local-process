"""Disguised IDs, dates and words for dummy data (processing/dummy_data.py).

Nothing is passed on unless it is on a list here: words are kept only when they are part of the
protocol's vocabulary, everything else becomes a placeholder. What the matching depends on
is kept instead: two names with one ID still share one, a twin's 12345-1 stays a twin, a typo stays
one character off, the order of the dates and times stays (not the dates, nor the gaps).
"""

import datetime as dt
import random
import re
import string
from collections.abc import Iterable

TASK_WORDS = ("cogreappraisal", "emolabeling")  # the matching finds the task by these
LOG_WORDS = frozenset({"scan", "log", "logs", "behav", "behavioural", "data", "task", "sub", "ses"})
# Words of scanner protocol names (the default scan rules and common sequences), lower case.
SERIES_WORDS = frozenset(
    "abcd ap pa ax axial asl bold checkerboard clinical cmrr cog cor coronal dce diff diffusion "
    "dir dti dwi ep2d epi field fieldmap flair fmri gre head iso labeling localizer localiser "
    "map mapping mb mbep2d medic mpr mprage mri nav nd neuro norm peer physiolog pilot rage "
    "reappraisal ref rest rms sag sagittal sbref scout se seq setter spinecho ss swi t1 t1w t2 "
    "t2w task tfmri tfl tra tse untitled vnav".split()
)
TWIN = re.compile(r"(?P<base>.*\d)(?P<dash>[-T])(?P<twin>\d)")
TOKEN = re.compile(r"\d{8}-\d{6}(?!\d)|[A-Za-z0-9]+|[^A-Za-z0-9]+")
SAFE_PUNCTUATION = set(" _-^.()+,'")
MIN_TYPO_LENGTH = 4  # shorter IDs one character apart are just different IDs
FIRST_FAKE_DAY = dt.date(2001, 1, 8)  # before any real scan: a fake date is never a real one
MAX_GAP_DAYS = 21
DAY_START_S, DAY_END_S = 7 * 3600, 20 * 3600
MARKERS = ("app", "Event")  # text the log reader looks for anywhere in a line


def one_character_off(a: str, b: str) -> bool:
    """One letter replaced, added or left out (or only the case differs)."""
    if a.casefold() == b.casefold():
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return a[i + 1 :] == b[i + 1 :] or a[i:] == b[i + 1 :] or a[i + 1 :] == b[i:]


class Disguise:
    """Learn every real ID, date and time first (learn_...), then ask for their disguises."""

    def __init__(self, rng: random.Random, keep_words: Iterable[str] = ()) -> None:
        self.rng = rng
        self.keep = {w.casefold() for w in keep_words}
        self.ids: dict[str, str] = {}  # real ID without punctuation or twin -> fake
        self.moments: dict[dt.date, set[dt.time]] = {}
        self.days: dict[dt.date, dt.date] = {}
        self.times: dict[dt.datetime, dt.datetime] = {}
        self.words: dict[str, str] = {}
        self.numbers: dict[str, str] = {}
        self.secrets: set[str] = set()  # real text that may not appear anywhere in the output

    # ---- IDs ----------------------------------------------------------------------------------

    @staticmethod
    def split_id(raw: str) -> tuple[str, str]:
        """('12345', '-1') for a twin, (raw, '') otherwise."""
        found = TWIN.fullmatch(raw)
        return (found["base"], found["dash"] + found["twin"]) if found else (raw, "")

    def learn_id(self, raw: str) -> None:
        base, _twin = self.split_id(raw.strip())
        core = "".join(c for c in base if c.isalnum())
        if core and core not in self.ids:
            self.secrets.add(core.casefold())
            self.ids[core] = ""  # made once all are known: never another real one

    def _random_like(self, text: str) -> str:
        return "".join(self._random_char(c) for c in text)

    def _random_char(self, c: str) -> str:
        if c.isdigit():
            return self.rng.choice(string.digits)
        return self.rng.choice(string.ascii_uppercase if c.isupper() else string.ascii_lowercase)

    def _acceptable(self, fake: str, real: str) -> bool:
        taken = {f.casefold() for f in self.ids.values()}
        return (
            fake.casefold() not in taken
            and fake.casefold() not in self.secrets
            and fake != real
            and not any(marker in fake for marker in MARKERS)
        )

    def _edited_like(self, real: str, relative: str) -> str:
        """The relative's fake with one random character changed as real differs from it."""
        fake = self.ids[relative]
        if real.casefold() == relative.casefold():
            return "".join(
                f.upper() if r.isupper() else f.lower() for f, r in zip(fake, real, strict=True)
            )
        i = 0
        while i < min(len(real), len(relative)) and real[i] == relative[i]:
            i += 1
        if len(real) == len(relative):
            return fake[:i] + self._random_char(real[i]) + fake[i + 1 :]
        if len(real) > len(relative):
            return fake[:i] + self._random_char(real[i]) + fake[i:]
        return fake[:i] + fake[i + 1 :]

    def _make_id(self, real: str) -> str:
        relative = next(
            (
                known
                for known, fake in self.ids.items()
                if fake and len(real) >= MIN_TYPO_LENGTH and one_character_off(real, known)
            ),
            None,
        )
        same_but_case = relative is not None and real.casefold() == relative.casefold()
        for attempt in range(1000):
            if relative is not None and attempt < 50:
                fake = self._edited_like(real, relative)
                if same_but_case:
                    return fake
            else:
                # Short IDs can run out: longer ones are always left.
                fake = self._random_like(real + "0" * (attempt // 200))
            if self._acceptable(fake, real):
                return fake
        raise ValueError("couldn't make up enough IDs")

    def fake_id(self, raw: str) -> str:
        """The ID's disguise, with its punctuation, spaces and twin ending as they are."""
        stripped = raw.strip()
        base, twin = self.split_id(stripped)
        core = "".join(c for c in base if c.isalnum())
        if not core:
            return stripped
        if not self.ids.get(core):
            self.learn_id(raw)
            self.ids[core] = self._make_id(core)
        rest = iter(self.ids[core])
        text = "".join(
            (next(rest, "") if c.isalnum() else c if c in SAFE_PUNCTUATION else "_") for c in base
        )
        return raw.replace(stripped, text + "".join(rest) + twin)

    # ---- dates and times ----------------------------------------------------------------------

    def learn_moment(self, day: dt.date, time: dt.time | None = None) -> None:
        self.moments.setdefault(day, set())
        if time is not None:
            self.moments[day].add(time)

    def settle(self) -> None:
        """Once everything is learnt: IDs, dates and times by their order only."""
        for core in sorted(self.ids, key=lambda c: (-len(c), c)):
            if not self.ids[core]:
                self.ids[core] = self._make_id(core)
        fake_day, before = FIRST_FAKE_DAY, None
        for day in sorted(self.moments):
            if before is not None:
                next_day = (day - before).days == 1  # e.g. a log written just after midnight
                fake_day += dt.timedelta(days=1 if next_day else self.rng.randint(2, MAX_GAP_DAYS))
            self.days[day], before = fake_day, day
            times = sorted(self.moments[day])
            seconds = sorted(self.rng.sample(range(DAY_START_S, DAY_END_S), len(times)))
            for time, second in zip(times, seconds, strict=True):
                start = dt.datetime.combine(fake_day, dt.time())
                fake = start + dt.timedelta(seconds=second)
                self.times[dt.datetime.combine(day, time)] = fake

    def fake_day(self, day: dt.date) -> dt.date:
        return self.days.get(day, FIRST_FAKE_DAY - dt.timedelta(days=1))

    def fake_moment(self, day: dt.date, time: dt.time) -> dt.datetime:
        known = self.times.get(dt.datetime.combine(day, time))
        return known or dt.datetime.combine(self.fake_day(day), dt.time(6))

    # ---- words --------------------------------------------------------------------------------

    def secret_words(self, text: str, allowed: frozenset[str]) -> None:
        """Words of a real name that may not come out: people's names, for example."""
        for word in re.findall(r"[A-Za-z]{4,}", text):
            lower = word.casefold()
            if lower not in allowed and lower not in self.keep and lower not in TASK_WORDS:
                self.secrets.add(lower)

    def placeholder(self, word: str, kind: str = "word") -> str:
        """Always the same one for a word; it keeps the markers the log reader looks for."""
        if word not in self.words:
            marks = "".join(f"_{m}" for m in MARKERS if m in word)
            self.words[word] = f"{kind}{len(self.words) + 1:03d}{marks}"
        return self.words[word]

    def number(self, digits: str) -> str:
        if digits not in self.numbers:
            fake = self._random_like(digits)
            while fake in self.secrets or (fake == digits and len(digits) > 1):
                fake = self._random_like(digits)
            self.numbers[digits] = fake
        return self.numbers[digits]

    def text(self, text: str, allowed: frozenset[str], keep_digits: int = 2) -> str:
        """Free text (a file or folder name): known IDs, dates and allowed words are handled as
        such, other words and longer numbers replaced."""
        return "".join(self._token(t, allowed, keep_digits) for t in TOKEN.findall(text))

    def _token(self, token: str, allowed: frozenset[str], keep_digits: int) -> str:
        if not token[0].isalnum():
            return "".join(c if c in SAFE_PUNCTUATION else "_" for c in token)
        if "-" in token:  # YYYYMMDD-HHMMSS
            try:
                stamp = dt.datetime.strptime(token, "%Y%m%d-%H%M%S")
            except ValueError:
                return "-".join(self.number(part) for part in token.split("-"))
            return f"{self.fake_moment(stamp.date(), stamp.time()):%Y%m%d-%H%M%S}"
        if token in self.ids:
            return self.fake_id(token)
        lower = token.casefold()
        if lower in self.keep or lower in allowed:
            return token
        for task in TASK_WORDS:
            if task in lower:
                at = lower.index(task)
                before, word = token[:at], token[at : at + len(task)]
                after = token[at + len(task) :]
                parts = (self._token(p, allowed, keep_digits) if p else "" for p in (before, after))
                return word.join(parts)
        if token.isdigit():
            if len(token) == 8:
                try:
                    day = dt.datetime.strptime(token, "%Y%m%d").date()
                except ValueError:
                    return "99999999"  # still no date
                return f"{self.fake_day(day):%Y%m%d}"
            return token if len(token) <= keep_digits else self.number(token)
        return self.placeholder(token)
