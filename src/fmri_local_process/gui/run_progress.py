"""The large 'Running' panel of a batch of Docker runs, one subject (or several at once) after
another: C-PAC on Preprocess, MRIQC on Scan quality.

    ◐  sub-01  -  step 120 of ~400                       [the owner's stop buttons]
    [================ 30%  (estimated from the last complete run) ================]
    sub-01: step 120 of ~400 - last finished: anat_skullstrip
    [====== Subjects: 1 of 4 done ======                                           ]

Progress is counted in pipeline steps (nipype nodes finished): the bar measures them against
how many the last complete run of a subject took (the owner remembers that count). Without
one yet, the bar is a moving 'busy' bar and the title counts the steps. Shown only while the
batch runs.
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from fmri_local_process.app.run_progress import SPINNER, BatchProgress
from fmri_local_process.gui.qt_common import (
    PROCESS_ACCENT_COLOR,
    accent_group_box_stylesheet,
    style_name_label,
    style_secondary_label,
)

SPINNER_MS = 150


class RunProgress(QGroupBox):
    def __init__(self, title: str = "Running") -> None:
        super().__init__(title)
        self.setStyleSheet(accent_group_box_stylesheet(PROCESS_ACCENT_COLOR))
        layout = QVBoxLayout(self)
        self.head = QHBoxLayout()  # the title, then the owner's buttons (add_button)
        self.title_label = QLabel()
        style_name_label(self.title_label)
        self.title_label.setStyleSheet(f"color: {PROCESS_ACCENT_COLOR};")
        self.head.addWidget(self.title_label, 1)
        layout.addLayout(self.head)

        self.step_bar = QProgressBar()
        self.step_bar.setMinimumHeight(30)
        bar_font = self.step_bar.font()
        bar_font.setPointSize(bar_font.pointSize() + 3)
        bar_font.setBold(True)
        self.step_bar.setFont(bar_font)
        self.step_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {PROCESS_ACCENT_COLOR}; }}"
            "QProgressBar { text-align: center; }"
        )
        layout.addWidget(self.step_bar)
        self.last_step_label = QLabel()
        style_secondary_label(self.last_step_label)
        layout.addWidget(self.last_step_label)
        self.subject_bar = QProgressBar()
        layout.addWidget(self.subject_bar)

        self.batch = BatchProgress()
        self.running = self.batch.running
        self.spinner_index = 0
        self.spinner_timer = QTimer(self)
        self.spinner_timer.setInterval(SPINNER_MS)
        self.spinner_timer.timeout.connect(self.update_title)
        self.setVisible(False)

    def add_button(self, button: QWidget) -> None:
        self.head.addWidget(button)

    @property
    def estimate(self) -> int | None:
        return self.batch.estimate

    @estimate.setter
    def estimate(self, steps: int | None) -> None:
        self.batch.estimate = steps

    @property
    def spinner(self) -> str:
        return SPINNER[self.spinner_index]

    # ---- the batch -----------------------------------------------------------------------------

    def start(self, n_subjects: int, estimate: int | None, stage: str) -> None:
        self.batch.start(n_subjects, estimate, stage)
        self.step_bar.setRange(0, 0)
        self.last_step_label.setText("")
        self.subject_bar.setRange(0, n_subjects)
        self.subject_bar.setValue(0)
        self.subject_bar.setFormat("Subjects: %v of %m done")
        self.setVisible(True)
        self.spinner_timer.start()
        self.update_title()

    def set_stage(self, stage: str) -> None:
        self.batch.stage = stage
        self.update_title()

    def subject_started(self, subject: str) -> None:
        self.batch.subject_started(subject)
        self.update_bars()
        self.update_title()

    def step(self, subject: str, steps: int, last_step: str) -> None:
        if not self.batch.step(subject, steps, last_step):
            return
        self.update_bars()
        self.update_title()

    def subject_finished(self, subject: str) -> None:
        self.batch.subject_finished(subject)
        self.subject_bar.setValue(self.batch.done)
        self.update_bars()
        self.update_title()

    def finish(self) -> None:
        self.running.clear()
        self.spinner_timer.stop()
        self.setVisible(False)

    # ---- showing -------------------------------------------------------------------------------

    def step_count(self, steps: int) -> str:
        return self.batch.step_count(steps)

    def update_title(self) -> None:
        self.spinner_index = (self.spinner_index + 1) % len(SPINNER)
        self.title_label.setText(f"{self.spinner}  {self.batch.title()}")

    def update_bars(self) -> None:
        """One subject: its steps; several: their average (against the last run's count)."""
        if not self.running:
            return
        if not self.estimate:
            self.step_bar.setRange(0, 0)  # first run: busy bar; the title shows steps
        else:
            mean = self.batch.mean_steps()
            self.step_bar.setRange(0, max(self.estimate, mean + 1))
            self.step_bar.setValue(mean)
            self.step_bar.setFormat(f"%p%{self.batch.bar_note()}")
        self.last_step_label.setText(self.batch.details())
