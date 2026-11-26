"""The progress of a batch of Docker runs: which subjects run, at which pipeline step, and the
texts of the 'Running' panel. Progress is counted in pipeline steps (nipype nodes finished),
measured against how many the last complete run of a subject took."""

import time
from dataclasses import dataclass

SPINNER = "◐◓◑◒"


@dataclass
class RunningSubject:
    started_at: float
    steps: int = 0
    last_step: str = ""


def step_count(steps: int, estimate: int | None) -> str:
    return f"step {steps} of ~{estimate}" if estimate else f"step {steps}"


class BatchProgress:
    def __init__(self) -> None:
        self.running: dict[str, RunningSubject] = {}  # in start order
        self.estimate: int | None = None  # steps a subject took last time (None: unknown)
        self.stage = ""  # shown while no subject runs, e.g. 'Checking Docker...'
        self.total = 0
        self.done = 0

    def start(self, n_subjects: int, estimate: int | None, stage: str) -> None:
        self.running.clear()  # cleared, not replaced: owners may hold this dict
        self.estimate, self.stage = estimate, stage
        self.total, self.done = n_subjects, 0

    def subject_started(self, subject: str) -> None:
        self.running[subject] = RunningSubject(time.monotonic())

    def step(self, subject: str, steps: int, last_step: str) -> bool:
        """False when that subject isn't running."""
        state = self.running.get(subject)
        if state is None:
            return False
        state.steps, state.last_step = steps, last_step
        return True

    def subject_finished(self, subject: str) -> None:
        self.running.pop(subject, None)
        if not self.running:
            self.stage = "Next subject..."
        self.done += 1

    def step_count(self, steps: int) -> str:
        return step_count(steps, self.estimate)

    def title(self) -> str:
        if not self.running:
            return self.stage
        if len(self.running) == 1:
            subject, state = next(iter(self.running.items()))
            return f"sub-{subject}  -  {self.step_count(state.steps)}"
        names = ", ".join(f"sub-{subject}" for subject in self.running)
        return f"{len(self.running)} at once: {names}"

    def mean_steps(self) -> int:
        """One subject: its steps; several: their average."""
        if not self.running:
            return 0
        return sum(state.steps for state in self.running.values()) // len(self.running)

    def bar_note(self) -> str:
        average = " on average" if len(self.running) > 1 else ""
        return f"{average}  (estimated from the last complete run)"

    def details(self) -> str:
        return "\n".join(
            f"sub-{subject}: {self.step_count(state.steps)}"
            f"{f' - last finished: {state.last_step}' if state.last_step else ''}"
            for subject, state in self.running.items()
        )

    def live_lines(self, spinner: str) -> list[str]:
        """'BUSY' lines for an activity log, one per running subject."""
        lines = []
        for subject, state in self.running.items():
            elapsed = int(time.monotonic() - state.started_at)
            count = f"step {state.steps}"
            if self.estimate:
                percent = min(99, 100 * state.steps // self.estimate)
                count += f" of ~{self.estimate} ({percent}%)"
            took = f"{elapsed // 60}m{elapsed % 60:02d}s"
            text = f"{spinner} BUSY sub-{subject}: {count}, {took}"
            lines.append(f"{text} - {state.last_step or 'starting container'}")
        return lines
