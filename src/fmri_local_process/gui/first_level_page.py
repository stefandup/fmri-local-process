"""The First-level stats tab: pick a setup (the checkerboard first), a subject and the regions,
fit the GLM to each of the subject's runs of that task, and compare them side by side. The
first subject is modelled at startup (auto_run), so the tab opens on results.

* Regions come from a standard atlas (Harvard-Oxford, Juelich, MNI lobes) copied out of the
  C-PAC image; tick them on and off. A setup ticks its focus regions first (occipital
  regions for the checkerboard). With no atlas, the whole brain is the region.
* The brain views show each run's t map above the threshold over the MNI template, the ticked
  regions outlined. Each run has its own cursor (starting at its own peak); click a view to
  move it. Lock cursors to move them all to the same MNI position.
* The time courses show the signal with the model's fit (ON blocks shaded) at each run's peak
  voxel, at the cursor, or as the region's mean; below them the response averaged over the
  ON/OFF cycles, all runs overlaid.
* The table compares the runs: peak and mean statistics, scan-length-fair measures, overlap
  between runs, the BIDS scan parameters (differences highlighted) and C-PAC's QC values.
"""

from pathlib import Path

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.app.first_level import (
    DIFFERS_COLOR,
    NO_ATLAS,
    SHOW_CURSOR,
    SHOW_PEAK,
    SHOW_REGION,
    Cursor,
    FirstLevelModel,
    ModelJob,
    differs,
    fetch_atlases,
    modelled_text,
    subject_notices,
)
from fmri_local_process.gui.qt_common import (
    PROCESS_ACCENT_COLOR,
    PROCESS_ACCENT_HOVER_COLOR,
    NoticeBanner,
    primary_action_stylesheet,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.gui.toolbox_state import ToolboxState
from fmri_local_process.processing import atlases as at
from fmri_local_process.processing import first_level as fl
from fmri_local_process.processing.cpac_docker import (
    CPAC_IMAGE,
)


class ModelThread(QThread):
    """Fits the design to every run (a few seconds each: the 4D file is read whole)."""

    done = Signal(list, str)  # results, error text

    def __init__(
        self, runs: list[fl.RunFile], design: fl.BlockDesign, reg: str, bids_root: Path | None
    ) -> None:
        super().__init__()
        self.job = ModelJob(runs, design, reg, bids_root)

    def run(self) -> None:
        self.done.emit(*self.job.run())


class FetchAtlasThread(QThread):
    done = Signal(str)  # error text, "" on success

    def run(self) -> None:
        self.done.emit(fetch_atlases())


class FirstLevelPage(QWidget):
    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        self.model = FirstLevelModel()
        self.worker: ModelThread | None = None
        self.formulas_dialog: QDialog | None = None
        self.fetcher: FetchAtlasThread | None = None
        self.seen_output: Path | None = None

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_controls())
        self.banner = NoticeBanner()
        layout.addWidget(self.banner)
        layout.addWidget(self._build_design())
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_regions())
        splitter.addWidget(self._build_results())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 1100])
        layout.addWidget(splitter, 1)
        self.on_setup_changed()

    # ---- building ------------------------------------------------------------------------------

    def _build_controls(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Setup:"))
        self.setup_box = QComboBox()
        for setup in fl.SETUPS:
            self.setup_box.addItem(setup.name, setup.key)
        self.setup_box.currentIndexChanged.connect(self.on_setup_changed)
        row.addWidget(self.setup_box)
        row.addWidget(QLabel("Subject:"))
        self.subject_box = QComboBox()
        self.subject_box.setMinimumWidth(120)
        row.addWidget(self.subject_box)
        row.addWidget(QLabel("Data:"))
        self.data_box = QComboBox()
        for reg, text in fl.DATA_CHOICES.items():
            self.data_box.addItem(text, reg)
        self.data_box.setToolTip(
            wrap_tooltip(
                "Which of C-PAC's denoised MNI-space BOLD series to model. Global signal "
                "regression (reg-1) can take some of a strong, widespread visual response "
                "with it, so reg-2 is the default."
            )
        )
        row.addWidget(self.data_box)
        row.addWidget(QLabel("t >"))
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.0, 50.0)
        self.threshold.setSingleStep(0.5)
        self.threshold.setValue(fl.DEFAULT_T_THRESHOLD)
        self.threshold.setToolTip("Voxels above this t count as active (map and table).")
        self.threshold.valueChanged.connect(self.update_stats)
        row.addWidget(self.threshold)
        self.run_button = QPushButton("Run first-level stats")
        self.run_button.setStyleSheet(
            primary_action_stylesheet(PROCESS_ACCENT_COLOR, PROCESS_ACCENT_HOVER_COLOR)
        )
        self.run_button.clicked.connect(self.on_run)
        row.addWidget(self.run_button)
        self.status = QLabel()
        style_secondary_label(self.status)
        row.addWidget(self.status, 1)
        return row

    def _build_design(self) -> QGroupBox:
        box = QGroupBox("Design")
        row = QHBoxLayout(box)
        row.addWidget(QLabel("First block:"))
        self.first_block = QComboBox()
        self.first_block.addItems(["off", "on"])
        row.addWidget(self.first_block)
        self.on_s, self.off_s, self.total_s = (self._seconds() for _ in range(3))
        for label, spin in (("ON (s):", self.on_s), ("OFF (s):", self.off_s)):
            row.addWidget(QLabel(label))
            row.addWidget(spin)
        row.addWidget(QLabel("Task length (s):"))
        row.addWidget(self.total_s)
        self.about = QLabel()
        style_secondary_label(self.about)
        self.about.setWordWrap(True)
        row.addWidget(self.about, 1)
        return box

    @staticmethod
    def _seconds() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.5, 3600.0)
        spin.setDecimals(1)
        return spin

    def _build_regions(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.atlas_box = QComboBox()
        self.atlas_box.addItem(NO_ATLAS, "")
        for spec in at.ATLASES:
            self.atlas_box.addItem(spec.name, spec.key)
        self.atlas_box.currentIndexChanged.connect(self.on_atlas_changed)
        layout.addWidget(self.atlas_box)
        self.fetch_button = QPushButton("Get atlases from the C-PAC image")
        self.fetch_button.setToolTip(
            wrap_tooltip(
                f"Copy FSL's atlases and the MNI152 template out of {CPAC_IMAGE} (Docker must "
                f"be running) into {self.model.cache}. Once only."
            )
        )
        self.fetch_button.clicked.connect(self.on_fetch_atlases)
        layout.addWidget(self.fetch_button)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter regions...")
        self.search.textChanged.connect(self.filter_regions)
        layout.addWidget(self.search)
        buttons = QHBoxLayout()
        for text, slot in (
            ("Focus", self.tick_focus),
            ("All", self.tick_all),
            ("None", self.tick_none),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.region_list = QListWidget()
        self.region_list.itemChanged.connect(self.on_region_ticked)
        layout.addWidget(self.region_list, 1)
        self.region_count = QLabel()
        style_secondary_label(self.region_count)
        layout.addWidget(self.region_count)
        return panel

    def _build_results(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Vertical)
        brain = QWidget()
        brain_layout = QVBoxLayout(brain)
        brain_layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        peak = QPushButton("Cursor to peak")
        peak.setToolTip(
            wrap_tooltip(
                "Each run's cursor to its own peak voxel. Locked: every run's cursor to the "
                "first run's peak."
            )
        )
        peak.clicked.connect(self.cursor_to_peak)
        row.addWidget(peak)
        self.lock_box = QCheckBox("Lock cursors")
        self.lock_box.setToolTip(
            wrap_tooltip(
                "Locked: clicking a run's view moves every run's cursor to the same MNI "
                "position (compare the runs at one spot). Unlocked: each run has its own cursor."
            )
        )
        self.lock_box.toggled.connect(self.on_lock_toggled)
        row.addWidget(self.lock_box)
        row.addWidget(QLabel("Time course:"))
        self.show_box = QComboBox()
        self.show_box.addItems([SHOW_PEAK, SHOW_CURSOR, SHOW_REGION])
        self.show_box.currentIndexChanged.connect(self.draw_time_courses)
        row.addWidget(self.show_box)
        self.cursor_label = QLabel()
        row.addWidget(self.cursor_label, 1)
        brain_layout.addLayout(row)
        self.brain_figure = Figure(facecolor="black")
        self.brain_canvas = FigureCanvasQTAgg(self.brain_figure)
        self.brain_canvas.mpl_connect("button_press_event", self.on_brain_click)
        brain_layout.addWidget(self.brain_canvas, 1)
        splitter.addWidget(brain)

        self.time_figure = Figure()
        self.time_canvas = FigureCanvasQTAgg(self.time_figure)
        splitter.addWidget(self.time_canvas)

        table_panel = QWidget()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(0, 0, 0, 0)
        buttons = QHBoxLayout()
        self.formulas_button = QPushButton("Metric formulas...")
        self.formulas_button.setToolTip(
            "How every metric in the table is calculated, with equations and references."
        )
        self.formulas_button.clicked.connect(self.on_show_formulas)
        buttons.addWidget(self.formulas_button)
        buttons.addStretch(1)
        self.save_button = QPushButton("Save maps (NIfTI)...")
        self.save_button.setToolTip("Write each run's beta, t, CNR (and % change) maps.")
        self.save_button.clicked.connect(self.on_save_maps)
        buttons.addWidget(self.save_button)
        self.export_button = QPushButton("Export CSV...")
        self.export_button.clicked.connect(self.on_export_csv)
        buttons.addWidget(self.export_button)
        table_layout.addLayout(buttons)
        self.table = QTableWidget(0, 0)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        table_layout.addWidget(self.table, 1)
        splitter.addWidget(table_panel)
        splitter.setSizes([420, 360, 300])
        return splitter

    # ---- setup, subjects, atlases --------------------------------------------------------------

    @property
    def setup(self) -> fl.FirstLevelSetup:
        return self.model.setup

    @property
    def atlas(self) -> at.Atlas | None:
        return self.model.atlas

    @property
    def results(self) -> list[fl.RunResult]:
        return self.model.results

    @property
    def stats(self) -> list[fl.RoiStats]:
        return self.model.stats

    @property
    def cursors(self) -> list[Cursor]:
        return self.model.cursors

    @property
    def axes_owner(self) -> dict:
        return self.model.axes_owner

    def design(self) -> fl.BlockDesign:
        return fl.BlockDesign(
            first_block=self.first_block.currentText(),
            on_s=self.on_s.value(),
            off_s=self.off_s.value(),
            total_s=self.total_s.value(),
        )

    def on_setup_changed(self) -> None:
        key = self.setup_box.currentData()
        setup = next(s for s in fl.SETUPS if s.key == key)
        first_atlas = self.model.use_setup(setup)
        design = setup.design
        self.first_block.setCurrentText(design.first_block)
        self.on_s.setValue(design.on_s)
        self.off_s.setValue(design.off_s)
        self.total_s.setValue(design.total_s)
        self.about.setText(self.setup.about)
        index = self.atlas_box.findData(first_atlas) if first_atlas else 0
        self.atlas_box.setCurrentIndex(max(index, 0))
        self.on_atlas_changed()
        self.fill_subjects()

    def show_page(self) -> None:
        output = self.state.folder("output")
        if output != self.seen_output:
            self.fill_subjects()
        self.fetch_button.setVisible(not at.atlases_present(self.model.cache))

    def fill_subjects(self) -> None:
        output = self.state.folder("output")
        self.seen_output = output
        current = self.subject_box.currentText()
        subjects = fl.output_subjects(output, self.setup.task) if output else []
        self.subject_box.clear()
        self.subject_box.addItems(subjects)
        if current in subjects:
            self.subject_box.setCurrentText(current)
        self.banner.set_messages(subject_notices(output, subjects, self.setup.task))
        self.status.setText(
            f"{len(subjects)} subject(s) with task-{self.setup.task}." if subjects else ""
        )
        self.run_button.setEnabled(bool(subjects))

    def on_fetch_atlases(self) -> None:
        if self.fetcher is not None:
            return
        self.fetch_button.setEnabled(False)
        self.status.setText("Copying the atlases out of the C-PAC image...")
        self.fetcher = FetchAtlasThread()
        self.fetcher.done.connect(self.on_atlases_fetched)
        self.fetcher.start()

    def on_atlases_fetched(self, error: str) -> None:
        if self.fetcher is not None:
            self.fetcher.wait()
        self.fetcher = None
        self.fetch_button.setEnabled(True)
        if error:
            QMessageBox.warning(self, "Atlases not copied", error)
            self.status.setText("Atlases not copied.")
            return
        self.status.setText(f"Atlases copied to {self.model.cache}.")
        self.fetch_button.setVisible(False)
        self.on_setup_changed()

    def on_atlas_changed(self) -> None:
        key = str(self.atlas_box.currentData() or "")
        problem = self.model.use_atlas(key)
        if problem and at.atlases_present(self.model.cache):
            QMessageBox.warning(self, "Atlas not read", problem)
        elif problem:
            self.status.setText(problem)
        self.fill_regions()
        self.update_stats()

    def fill_regions(self) -> None:
        self.region_list.blockSignals(True)
        self.region_list.clear()
        ticked_values = self.model.ticked_values()
        for region in self.model.regions():
            item = QListWidgetItem(region.name)
            item.setData(Qt.ItemDataRole.UserRole, region.value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            ticked = region.value in ticked_values
            item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
            self.region_list.addItem(item)
        self.region_list.blockSignals(False)
        self.filter_regions()
        self.update_region_count()

    def filter_regions(self) -> None:
        words = self.search.text().lower().split()
        for row in range(self.region_list.count()):
            item = self.region_list.item(row)
            item.setHidden(not all(word in item.text().lower() for word in words))

    def update_region_count(self) -> None:
        self.region_count.setText(self.model.region_text())

    def on_region_ticked(self, item: QListWidgetItem) -> None:
        if self.atlas is None:
            return
        values = set(self.model.ticked_values())
        value = int(item.data(Qt.ItemDataRole.UserRole))
        if item.checkState() == Qt.CheckState.Checked:
            values.add(value)
        else:
            values.discard(value)
        self.model.tick(values)
        self.update_region_count()
        self.update_stats()

    def _tick(self, values: set[int]) -> None:
        if self.atlas is None:
            return
        self.model.tick(values)
        self.fill_regions()
        self.update_stats()

    def tick_focus(self) -> None:
        if self.atlas is not None:
            self._tick(self.model.focus_regions())

    def tick_all(self) -> None:
        if self.atlas is not None:
            self._tick({r.value for r in self.atlas.regions})

    def tick_none(self) -> None:
        self._tick(set())

    # ---- running -------------------------------------------------------------------------------

    def auto_run(self) -> bool:
        """At startup: model the first subject at once, so the tab opens on results rather
        than on a button newcomers may not know to press. True when it started."""
        output = self.state.folder("output")
        subject = self.subject_box.currentText()
        if output is None or not subject or self.worker is not None or self.results:
            return False
        reg = str(self.data_box.currentData())
        if not fl.find_runs(output, subject, self.setup.task, reg):
            return False
        self.on_run()
        return self.worker is not None

    def on_run(self) -> None:
        output = self.state.folder("output")
        subject = self.subject_box.currentText()
        if output is None or not subject or self.worker is not None:
            return
        reg = str(self.data_box.currentData())
        runs = fl.find_runs(output, subject, self.setup.task, reg)
        if not runs:
            QMessageBox.information(
                self, "Nothing to model", f"No reg-{reg} MNI-space task-{self.setup.task} BOLD."
            )
            return
        self.run_button.setEnabled(False)
        self.status.setText(f"Modelling {len(runs)} run(s) of sub-{subject}...")
        self.worker = ModelThread(runs, self.design(), reg, self.state.folder("bids"))
        self.worker.done.connect(self.on_modelled)
        self.worker.start()

    def on_modelled(self, results: list, errors: str) -> None:
        if self.worker is not None:
            self.worker.wait()
        self.worker = None
        self.run_button.setEnabled(True)
        self.model.set_results(results)
        if errors:
            QMessageBox.warning(self, "Some runs not modelled", errors)
        self.status.setText(modelled_text(results))
        self.update_stats()
        self.cursor_to_peak()

    def update_stats(self) -> None:
        self.model.threshold = self.threshold.value()
        if self.results:
            self.model.update_stats()
        self.fill_table()
        self.draw_brain()
        self.draw_time_courses()

    # ---- cursor and drawing -------------------------------------------------------------------

    def cursor_to_peak(self) -> None:
        """Unlocked: each run's cursor to its own peak. Locked: all to the first run's."""
        self.model.cursors_to_peak()
        self.draw_brain()
        self.draw_time_courses()

    def on_lock_toggled(self, locked: bool) -> None:
        """Locking brings every run's cursor to the one moved last."""
        self.model.lock(locked)
        if locked and self.cursors:
            self.draw_brain()
            self.draw_time_courses()

    def draw_brain(self) -> None:
        text = self.model.draw_brain(self.brain_figure)
        if text:
            self.cursor_label.setText(text)
        self.brain_canvas.draw_idle()

    def on_brain_click(self, event) -> None:  # noqa: ANN001 -- matplotlib MouseEvent
        if event.xdata is None or event.ydata is None:
            return
        if not self.model.click(event.inaxes, event.xdata, event.ydata):
            return
        self.show_box.setCurrentText(SHOW_CURSOR)
        self.draw_brain()
        self.draw_time_courses()

    def draw_time_courses(self) -> None:
        self.model.draw_time_courses(self.time_figure, self.show_box.currentText())
        self.time_canvas.draw_idle()

    # ---- the comparison table -----------------------------------------------------------------

    def fill_table(self) -> None:
        rows = self.model.comparison()
        self.table.clear()
        self.table.setRowCount(len(rows))
        self.table.setColumnCount(len(self.results))
        self.table.setHorizontalHeaderLabels([r.run.label for r in self.results])
        self.table.setVerticalHeaderLabels([name for name, _ in rows])
        black = QColor("black")
        for row, (name, values) in enumerate(rows):
            header = self.table.verticalHeaderItem(row)
            if header is not None and name in fl.METRIC_HELP:
                header.setToolTip(wrap_tooltip(fl.METRIC_HELP[name]))
            highlight = differs(name, values)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if highlight:
                    item.setBackground(QColor(DIFFERS_COLOR))
                    item.setForeground(black)
                    item.setToolTip("Differs between the runs")
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.export_button.setEnabled(bool(rows))
        self.save_button.setEnabled(bool(self.results))

    def on_show_formulas(self) -> None:
        from fmri_local_process.gui.metric_help_dialog import MetricHelpDialog

        if self.formulas_dialog is None:  # rendering the equations takes a moment: once
            self.formulas_dialog = MetricHelpDialog(self)
        self.formulas_dialog.show()
        self.formulas_dialog.raise_()

    def on_export_csv(self) -> None:
        rows = self.model.comparison()
        output = self.state.folder("output")
        if not rows or output is None:
            return
        start = str(output / self.model.csv_name())
        chosen, _ = QFileDialog.getSaveFileName(self, "Export CSV", start, "CSV files (*.csv)")
        if not chosen:
            return
        try:
            self.model.export_csv(Path(chosen))
        except OSError as e:
            QMessageBox.warning(self, "Not exported", str(e))

    def on_save_maps(self) -> None:
        output = self.state.folder("output")
        if not self.results or output is None:
            return
        try:
            written, folder = self.model.save_maps(output)
        except OSError as e:
            QMessageBox.warning(self, "Maps not saved", str(e))
            return
        QMessageBox.information(self, "Saved", f"{written} map(s) saved in {folder}")
