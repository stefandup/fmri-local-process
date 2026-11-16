"""The First-level stats tab: pick a setup (the checkerboard first), a subject and the regions,
fit the GLM to each of the subject's runs of that task, and compare them side by side.

* Regions come from a standard atlas (Harvard-Oxford, Juelich, MNI lobes) copied out of the
  C-PAC image; tick them on and off. A setup ticks its focus regions first (occipital
  regions for the checkerboard). With no atlas, the whole brain is the region.
* The brain views show each run's t map above the threshold over the MNI template, the ticked
  regions outlined. Click a view to move the cursor.
* The time courses show the signal with the model's fit (ON blocks shaded) at each run's peak
  voxel, at the cursor, or as the region's mean; below them the response averaged over the
  ON/OFF cycles, all runs overlaid.
* The table compares the runs: peak and mean statistics, scan-length-fair measures, overlap
  between runs, the BIDS scan parameters (differences highlighted) and C-PAC's QC values.
"""

import csv
from pathlib import Path

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.gui.qt_common import (
    PROCESS_ACCENT_COLOR,
    PROCESS_ACCENT_HOVER_COLOR,
    primary_action_stylesheet,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.gui.toolbox_state import ToolboxState
from fmri_local_process.processing import atlases as at
from fmri_local_process.processing import first_level as fl
from fmri_local_process.processing.cpac_docker import (
    CPAC_IMAGE,
    DockerUnavailableError,
    find_docker,
)

VIEWS = (("Sagittal", 0), ("Coronal", 1), ("Axial", 2))
RUN_COLORS = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e")
ROI_COLOR = "#00e5ff"
DIFFERS_COLOR = "#f9e79f"
SHOW_PEAK, SHOW_CURSOR, SHOW_REGION = (
    "Peak voxel (each run's own)",
    "Voxel at cursor",
    "Region mean",
)
NO_ATLAS = "No atlas: whole brain"


class ModelThread(QThread):
    """Fits the design to every run (a few seconds each: the 4D file is read whole)."""

    done = Signal(list, str)  # results, error text

    def __init__(
        self, runs: list[fl.RunFile], design: fl.BlockDesign, reg: str, bids_root: Path | None
    ) -> None:
        super().__init__()
        self.runs, self.design, self.reg, self.bids_root = runs, design, reg, bids_root

    def run(self) -> None:
        results, errors = [], []
        for run in self.runs:
            try:
                result = fl.model_run(run, self.design)
            except (fl.FirstLevelError, MemoryError) as e:
                errors.append(str(e))
                continue
            result.extra.update(fl.scan_details(self.bids_root, run))
            result.extra.update(fl.cpac_qc_values(run, self.reg))
            results.append(result)
        self.done.emit(results, "\n".join(errors))


class FetchAtlasThread(QThread):
    done = Signal(str)  # error text, "" on success

    def run(self) -> None:
        try:
            at.fetch_atlases(find_docker(), CPAC_IMAGE, at.default_cache())
        except (at.AtlasError, DockerUnavailableError, OSError) as e:
            self.done.emit(str(e))
            return
        self.done.emit("")


class FirstLevelPage(QWidget):
    def __init__(self, state: ToolboxState) -> None:
        super().__init__()
        self.state = state
        self.cache = at.default_cache()
        self.atlas: at.Atlas | None = None
        self.template: tuple[np.ndarray, np.ndarray] | None = None
        self.ticked: dict[str, set[int]] = {}  # atlas key -> ticked region values
        self.results: list[fl.RunResult] = []
        self.stats: list[fl.RoiStats] = []
        self.on_grid: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}  # grid -> (bg, labels)
        self.cursor_mm: tuple[float, float, float] | None = None
        self.worker: ModelThread | None = None
        self.fetcher: FetchAtlasThread | None = None
        self.seen_output: Path | None = None

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_controls())
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
        row.addWidget(QLabel("Task starts at volume:"))
        self.start_volume = QSpinBox()
        self.start_volume.setRange(0, 100)
        self.start_volume.setToolTip(
            wrap_tooltip(
                "0-based volume of the first task trigger: the number of dummy triggers in "
                "task_config.toml (they are recorded as volumes, then the task starts)."
            )
        )
        row.addWidget(self.start_volume)
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
                f"be running) into {self.cache}. Once only."
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
        peak.clicked.connect(self.cursor_to_peak)
        row.addWidget(peak)
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
        key = self.setup_box.currentData()
        return next(s for s in fl.SETUPS if s.key == key)

    def design(self) -> fl.BlockDesign:
        return fl.BlockDesign(
            first_block=self.first_block.currentText(),
            on_s=self.on_s.value(),
            off_s=self.off_s.value(),
            total_s=self.total_s.value(),
            start_volume=self.start_volume.value(),
        )

    def on_setup_changed(self) -> None:
        design = self.setup.design
        self.first_block.setCurrentText(design.first_block)
        self.on_s.setValue(design.on_s)
        self.off_s.setValue(design.off_s)
        self.total_s.setValue(design.total_s)
        self.start_volume.setValue(design.start_volume)
        self.about.setText(self.setup.about)
        self.ticked.clear()
        first_atlas = next(iter(self.setup.focus), "")
        index = self.atlas_box.findData(first_atlas) if at.atlases_present(self.cache) else 0
        self.atlas_box.setCurrentIndex(max(index, 0))
        self.on_atlas_changed()
        self.fill_subjects()

    def show_page(self) -> None:
        output = self.state.folder("output")
        if output != self.seen_output:
            self.fill_subjects()
        self.fetch_button.setVisible(not at.atlases_present(self.cache))

    def fill_subjects(self) -> None:
        output = self.state.folder("output")
        self.seen_output = output
        current = self.subject_box.currentText()
        subjects = fl.output_subjects(output, self.setup.task) if output else []
        self.subject_box.clear()
        self.subject_box.addItems(subjects)
        if current in subjects:
            self.subject_box.setCurrentText(current)
        if output is None:
            self.status.setText("Choose the C-PAC output folder on the Preprocess tab.")
        elif not subjects:
            self.status.setText(f"No preprocessed task-{self.setup.task} runs in {output}.")
        else:
            self.status.setText(f"{len(subjects)} subject(s) with task-{self.setup.task}.")
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
        self.status.setText(f"Atlases copied to {self.cache}.")
        self.fetch_button.setVisible(False)
        self.on_setup_changed()

    def on_atlas_changed(self) -> None:
        key = self.atlas_box.currentData()
        self.atlas = None
        self.on_grid.clear()
        if key and at.atlases_present(self.cache):
            try:
                self.atlas = at.load_atlas(self.cache, key)
                if self.template is None:
                    self.template = at.load_template(self.cache)
            except (at.AtlasError, OSError) as e:
                QMessageBox.warning(self, "Atlas not read", str(e))
        elif key:
            self.status.setText("Get the atlases from the C-PAC image first.")
        self.fill_regions()
        self.update_stats()

    def fill_regions(self) -> None:
        self.region_list.blockSignals(True)
        self.region_list.clear()
        if self.atlas is not None:
            key = self.atlas.spec.key
            if key not in self.ticked:
                keywords = self.setup.focus.get(key, ())
                self.ticked[key] = at.matching_regions(self.atlas.regions, keywords)
            for region in sorted(self.atlas.regions, key=lambda r: r.name):
                item = QListWidgetItem(region.name)
                item.setData(Qt.ItemDataRole.UserRole, region.value)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                ticked = region.value in self.ticked[key]
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

    def ticked_values(self) -> set[int]:
        return set() if self.atlas is None else self.ticked.get(self.atlas.spec.key, set())

    def update_region_count(self) -> None:
        if self.atlas is None:
            self.region_count.setText("Region: the whole brain (no atlas)")
        else:
            self.region_count.setText(
                f"{len(self.ticked_values())} of {len(self.atlas.regions)} regions ticked"
            )

    def on_region_ticked(self, item: QListWidgetItem) -> None:
        if self.atlas is None:
            return
        values = self.ticked.setdefault(self.atlas.spec.key, set())
        value = int(item.data(Qt.ItemDataRole.UserRole))
        if item.checkState() == Qt.CheckState.Checked:
            values.add(value)
        else:
            values.discard(value)
        self.update_region_count()
        self.update_stats()

    def _tick(self, values: set[int]) -> None:
        if self.atlas is None:
            return
        self.ticked[self.atlas.spec.key] = values
        self.fill_regions()
        self.update_stats()

    def tick_focus(self) -> None:
        if self.atlas is not None:
            keywords = self.setup.focus.get(self.atlas.spec.key, ())
            self._tick(at.matching_regions(self.atlas.regions, keywords))

    def tick_all(self) -> None:
        if self.atlas is not None:
            self._tick({r.value for r in self.atlas.regions})

    def tick_none(self) -> None:
        self._tick(set())

    # ---- running -------------------------------------------------------------------------------

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
        self.results = results
        self.on_grid.clear()
        self.cursor_mm = None
        if errors:
            QMessageBox.warning(self, "Some runs not modelled", errors)
        self.status.setText(
            f"{len(results)} run(s) modelled: " + ", ".join(r.run.label for r in results)
        )
        self.update_stats()
        self.cursor_to_peak()

    def grid_images(self, result: fl.RunResult) -> tuple[np.ndarray | None, np.ndarray | None]:
        """The template and atlas labels on the run's voxel grid (cached per grid)."""
        if self.atlas is None or self.template is None:
            return None, None
        key = (result.mask.shape, result.affine.round(3).tobytes(), self.atlas.spec.key)
        if key not in self.on_grid:
            background = at.resample_nearest(
                self.template[0], self.template[1], result.mask.shape, result.affine
            )
            labels = at.resample_nearest(
                self.atlas.data, self.atlas.affine, result.mask.shape, result.affine
            )
            self.on_grid[key] = (background, labels)
        return self.on_grid[key]

    def region_on(self, result: fl.RunResult) -> np.ndarray:
        """The ticked regions on the run's grid; the whole brain with no atlas."""
        _background, labels = self.grid_images(result)
        if labels is None:
            return result.mask.copy()
        values = list(self.ticked_values())
        return np.isin(labels, values) if values else np.zeros(labels.shape, bool)

    def update_stats(self) -> None:
        if not self.results:
            self.fill_table()
            self.draw_brain()
            self.draw_time_courses()
            return
        threshold = self.threshold.value()
        self.stats = [fl.roi_stats(r, self.region_on(r), threshold) for r in self.results]
        self.fill_table()
        self.draw_brain()
        self.draw_time_courses()

    # ---- cursor and drawing -------------------------------------------------------------------

    def cursor_to_peak(self) -> None:
        peaks = [s.peak_mm for s in self.stats if s.peak_mm is not None]
        self.cursor_mm = peaks[0] if peaks else None
        self.draw_brain()
        self.draw_time_courses()

    def cursor_ijk(self, result: fl.RunResult) -> tuple[int, int, int]:
        if self.cursor_mm is None:
            return tuple(int(n // 2) for n in result.mask.shape)  # type: ignore[return-value]
        inverse = np.linalg.inv(result.affine)
        ijk = inverse[:3, :3] @ np.asarray(self.cursor_mm) + inverse[:3, 3]
        clipped = np.clip(np.round(ijk).astype(int), 0, np.asarray(result.mask.shape) - 1)
        return int(clipped[0]), int(clipped[1]), int(clipped[2])

    def draw_brain(self) -> None:
        figure = self.brain_figure
        figure.clear()
        if not self.results:
            figure.text(
                0.5,
                0.5,
                "Run first-level stats to see the maps",
                color="white",
                ha="center",
                va="center",
            )
            self.brain_canvas.draw_idle()
            return
        threshold = self.threshold.value()
        axes_grid = figure.subplots(len(self.results), len(VIEWS), squeeze=False)
        self.axes_owner: dict = {}
        cursor_text = []
        for row, result in enumerate(self.results):
            background, _labels = self.grid_images(result)
            if background is None:
                background = np.where(result.mask, 1.0, 0.0)
            region = self.region_on(result)
            ijk = self.cursor_ijk(result)
            t_here = float(result.t[ijk])
            cursor_text.append(f"{result.run.label}: t={t_here:.1f}")
            for column, (view, axis) in enumerate(VIEWS):
                axes = axes_grid[row][column]
                axes.set_axis_off()
                self.axes_owner[axes] = (row, axis)
                shown = [a for a in range(3) if a != axis]
                base = np.take(background, ijk[axis], axis=axis).T
                axes.imshow(base, cmap="gray", origin="lower", interpolation="nearest")
                t_slice = np.take(result.t, ijk[axis], axis=axis).T
                masked = np.ma.masked_where(t_slice <= threshold, t_slice)
                axes.imshow(
                    masked,
                    cmap="hot",
                    origin="lower",
                    interpolation="nearest",
                    vmin=threshold,
                    vmax=max(threshold * 3, float(result.t.max())),
                )
                roi_slice = np.take(region, ijk[axis], axis=axis).T
                if roi_slice.any() and not roi_slice.all():
                    axes.contour(
                        roi_slice.astype(float), levels=[0.5], colors=ROI_COLOR, linewidths=0.7
                    )
                axes.axvline(ijk[shown[0]], color="#55ff55", linewidth=0.5)
                axes.axhline(ijk[shown[1]], color="#55ff55", linewidth=0.5)
                if column == 0:
                    axes.set_title(result.run.label, color="white", fontsize=9, loc="left")
                elif row == 0:
                    axes.set_title(view, color="white", fontsize=9)
        if self.cursor_mm is not None:
            x, y, z = self.cursor_mm
            self.cursor_label.setText(
                f"Cursor MNI {x:.0f}, {y:.0f}, {z:.0f} mm   " + "   ".join(cursor_text)
            )
        figure.subplots_adjust(
            left=0.01, right=0.99, bottom=0.01, top=0.93, wspace=0.02, hspace=0.15
        )
        self.brain_canvas.draw_idle()

    def on_brain_click(self, event) -> None:  # noqa: ANN001 -- matplotlib MouseEvent
        owner = getattr(self, "axes_owner", {}).get(event.inaxes)
        if owner is None or event.xdata is None or event.ydata is None:
            return
        row, axis = owner
        result = self.results[row]
        ijk = list(self.cursor_ijk(result))
        shown = [a for a in range(3) if a != axis]
        ijk[shown[0]] = int(round(event.xdata))
        ijk[shown[1]] = int(round(event.ydata))
        self.cursor_mm = result.world((ijk[0], ijk[1], ijk[2]))
        self.show_box.setCurrentText(SHOW_CURSOR)
        self.draw_brain()
        self.draw_time_courses()

    def series_for(self, row: int) -> tuple[np.ndarray | None, str]:
        result, stats = self.results[row], self.stats[row] if self.stats else None
        mode = self.show_box.currentText()
        if mode == SHOW_REGION:
            return fl.roi_mean_series(result, self.region_on(result)), "region mean"
        if mode == SHOW_PEAK and stats is not None and stats.peak is not None:
            x, y, z = stats.peak_mm or (0, 0, 0)
            return result.voxel_series(stats.peak), f"peak voxel ({x:.0f}, {y:.0f}, {z:.0f})"
        ijk = self.cursor_ijk(result)
        x, y, z = result.world(ijk)
        return result.voxel_series(ijk), f"cursor voxel ({x:.0f}, {y:.0f}, {z:.0f})"

    def draw_time_courses(self) -> None:
        figure = self.time_figure
        figure.clear()
        if not self.results:
            self.time_canvas.draw_idle()
            return
        n = len(self.results)
        grid = figure.add_gridspec(n, 2, width_ratios=(3, 1))
        average_axes = figure.add_subplot(grid[:, 1])
        unit = ""
        for row, result in enumerate(self.results):
            color = RUN_COLORS[row % len(RUN_COLORS)]
            axes = figure.add_subplot(grid[row, 0])
            y, where = self.series_for(row)
            start = result.design.start_volume * result.tr
            for onset, duration in result.design.on_blocks():
                axes.axvspan(
                    start + onset, start + onset + duration, color="#ffd54f", alpha=0.3, linewidth=0
                )
            if y is None:
                axes.text(0.5, 0.5, "no signal here", transform=axes.transAxes, ha="center")
                continue
            times = np.arange(y.size) * result.tr
            axes.plot(times, y, color=color, linewidth=0.8, label="signal")
            axes.plot(times, fl.fitted(result, y), color="black", linewidth=1.2, label="GLM fit")
            axes.set_title(f"{result.run.label}: {where}", fontsize=8, loc="left")
            axes.tick_params(labelsize=7)
            if row == n - 1:
                axes.set_xlabel("time from scan start (s)", fontsize=8)
            if row == 0:
                axes.legend(fontsize=7, loc="upper right")
            centres, means, sems, unit = fl.block_average(result, y)
            average_axes.plot(centres, means, color=color, label=result.run.label)
            average_axes.fill_between(centres, means - sems, means + sems, color=color, alpha=0.2)
        first = self.results[0].design
        on_start = 0.0 if first.first_block == "on" else first.off_s
        average_axes.axvspan(
            on_start, on_start + first.on_s, color="#ffd54f", alpha=0.3, linewidth=0
        )
        average_axes.axhline(0, color="grey", linewidth=0.5)
        average_axes.set_title("Average ON/OFF cycle", fontsize=8)
        average_axes.set_xlabel("s into the cycle", fontsize=8)
        average_axes.set_ylabel(unit, fontsize=8)
        average_axes.tick_params(labelsize=7)
        if average_axes.get_legend_handles_labels()[0]:  # nothing drawn without a region
            average_axes.legend(fontsize=7)
        figure.tight_layout()
        self.time_canvas.draw_idle()

    # ---- the comparison table -----------------------------------------------------------------

    def comparison(self) -> list[tuple[str, list[str]]]:
        return fl.comparison_rows(self.results, self.stats) if self.results else []

    def fill_table(self) -> None:
        rows = self.comparison()
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
            differs = name in fl.SCAN_FIELDS and len(set(values)) > 1
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if differs:
                    item.setBackground(QColor(DIFFERS_COLOR))
                    item.setForeground(black)
                    item.setToolTip("Differs between the runs")
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.export_button.setEnabled(bool(rows))
        self.save_button.setEnabled(bool(self.results))

    def on_export_csv(self) -> None:
        rows = self.comparison()
        output = self.state.folder("output")
        if not rows or output is None:
            return
        subject = self.results[0].run.subject
        start = str(output / f"sub-{subject}_{self.setup.key}_first_level.csv")
        chosen, _ = QFileDialog.getSaveFileName(self, "Export CSV", start, "CSV files (*.csv)")
        if not chosen:
            return
        try:
            with Path(chosen).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["metric", *[r.run.label for r in self.results]])
                writer.writerows([name, *values] for name, values in rows)
        except OSError as e:
            QMessageBox.warning(self, "Not exported", str(e))

    def on_save_maps(self) -> None:
        output = self.state.folder("output")
        if not self.results or output is None:
            return
        subject = self.results[0].run.subject
        folder = output / "first_level" / f"sub-{subject}" / self.setup.key
        try:
            written = [p for r in self.results for p in fl.save_maps(r, folder)]
        except OSError as e:
            QMessageBox.warning(self, "Maps not saved", str(e))
            return
        QMessageBox.information(self, "Saved", f"{len(written)} map(s) saved in {folder}")
