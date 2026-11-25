"""First-level stats: fitting the runs, the regions and cursors, the figures and the comparison
table. The window draws the figures on its canvases, the terminal app saves them as PNGs."""

import csv
from pathlib import Path

import numpy as np
from matplotlib.axes import Axes
from matplotlib.figure import Figure

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

Cursor = tuple[float, float, float] | None  # MNI mm; None: the volume's centre


class ModelJob:
    """Fits the design to every run (a few seconds each: the 4D file is read whole)."""

    def __init__(
        self, runs: list[fl.RunFile], design: fl.BlockDesign, reg: str, bids_root: Path | None
    ) -> None:
        self.runs, self.design, self.reg, self.bids_root = runs, design, reg, bids_root

    def run(self) -> tuple[list[fl.RunResult], str]:
        """(results, error text)"""
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
        return results, "\n".join(errors)


def fetch_atlases() -> str:
    """Copy the atlases out of the C-PAC image: why not, "" when done."""
    try:
        at.fetch_atlases(find_docker(), CPAC_IMAGE, at.default_cache())
    except (at.AtlasError, DockerUnavailableError, OSError) as e:
        return str(e)
    return ""


def subject_notices(output: Path | None, subjects: list[str], task: str) -> list[str]:
    if output is None:
        return ["No C-PAC output folder chosen: choose it on the Preprocess BIDS (C-PAC) tab."]
    if not subjects:
        return [
            f"No preprocessed task-{task} runs in {output}: preprocess them on "
            "the Preprocess BIDS (C-PAC) tab first."
        ]
    return []


class FirstLevelModel:
    """The modelled runs, the atlas and its ticked regions, and where each run's cursor is."""

    def __init__(self) -> None:
        self.cache = at.default_cache()
        self.setup = fl.SETUPS[0]
        self.atlas: at.Atlas | None = None
        self.template: tuple[np.ndarray, np.ndarray] | None = None
        self.ticked: dict[str, set[int]] = {}  # atlas key -> ticked region values
        self.results: list[fl.RunResult] = []
        self.stats: list[fl.RoiStats] = []
        self.on_grid: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}  # grid -> (bg, labels)
        # One cursor per run. Locked: a click moves all.
        self.cursors: list[Cursor] = []
        self.last_cursor_row = 0  # the run whose cursor moved last: locking copies it
        self.locked = False
        self.threshold = fl.DEFAULT_T_THRESHOLD
        self.axes_owner: dict[Axes, tuple[int, int]] = {}  # drawn view -> (run, axis)

    # ---- atlas and regions --------------------------------------------------------------------

    def use_setup(self, setup: fl.FirstLevelSetup) -> str:
        """Start on this setup; the atlas it looks at first ('' for none)."""
        self.setup = setup
        self.ticked.clear()
        return next(iter(setup.focus), "") if at.atlases_present(self.cache) else ""

    def use_atlas(self, key: str) -> str:
        """Why the atlas (''  for none) couldn't be read, '' when it was."""
        self.atlas = None
        self.on_grid.clear()
        if not key:
            return ""
        if not at.atlases_present(self.cache):
            return "Get the atlases from the C-PAC image first."
        try:
            self.atlas = at.load_atlas(self.cache, key)
            if self.template is None:
                self.template = at.load_template(self.cache)
        except (at.AtlasError, OSError) as e:
            return str(e)
        self.ticked.setdefault(key, self.focus_regions())
        return ""

    def focus_regions(self) -> set[int]:
        if self.atlas is None:
            return set()
        keywords = self.setup.focus.get(self.atlas.spec.key, ())
        return at.matching_regions(self.atlas.regions, keywords)

    def regions(self) -> list[at.Region]:
        return sorted(self.atlas.regions, key=lambda r: r.name) if self.atlas else []

    def ticked_values(self) -> set[int]:
        return set() if self.atlas is None else self.ticked.get(self.atlas.spec.key, set())

    def tick(self, values: set[int]) -> None:
        if self.atlas is not None:
            self.ticked[self.atlas.spec.key] = values

    def region_text(self) -> str:
        if self.atlas is None:
            return "Region: the whole brain (no atlas)"
        return f"{len(self.ticked_values())} of {len(self.atlas.regions)} regions ticked"

    # ---- results ------------------------------------------------------------------------------

    def set_results(self, results: list[fl.RunResult]) -> None:
        self.results = results
        self.on_grid.clear()
        self.cursors = [None] * len(results)
        self.last_cursor_row = 0

    def update_stats(self) -> None:
        self.stats = [fl.roi_stats(r, self.region_on(r), self.threshold) for r in self.results]

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

    # ---- cursors ------------------------------------------------------------------------------

    def cursors_to_peak(self) -> None:
        """Unlocked: each run's cursor to its own peak. Locked: all to the first run's."""
        peaks = [s.peak_mm for s in self.stats]
        if self.locked:
            first = next((p for p in peaks if p is not None), None)
            self.cursors = [first] * len(self.results)
        else:
            self.cursors = list(peaks)

    def lock(self, locked: bool) -> None:
        """Locking brings every run's cursor to the one moved last."""
        self.locked = locked
        if locked and self.cursors:
            self.cursors = [self.cursors[self.last_cursor_row]] * len(self.cursors)

    def move_cursor(self, row: int, cursor: Cursor) -> None:
        if self.locked:
            self.cursors = [cursor] * len(self.results)
        else:
            self.cursors[row] = cursor
        self.last_cursor_row = row

    def click(self, axes: Axes, x: float, y: float) -> bool:
        """A click on a drawn view: True when it moved a cursor."""
        owner = self.axes_owner.get(axes)
        if owner is None:
            return False
        row, axis = owner
        ijk = list(self.cursor_ijk(row))
        shown = [a for a in range(3) if a != axis]
        ijk[shown[0]], ijk[shown[1]] = int(round(x)), int(round(y))
        self.move_cursor(row, self.results[row].world((ijk[0], ijk[1], ijk[2])))
        return True

    def cursor_ijk(self, row: int) -> tuple[int, int, int]:
        result = self.results[row]
        cursor = self.cursors[row] if row < len(self.cursors) else None
        if cursor is None:
            return tuple(int(n // 2) for n in result.mask.shape)  # type: ignore[return-value]
        inverse = np.linalg.inv(result.affine)
        ijk = inverse[:3, :3] @ np.asarray(cursor) + inverse[:3, 3]
        clipped = np.clip(np.round(ijk).astype(int), 0, np.asarray(result.mask.shape) - 1)
        return int(clipped[0]), int(clipped[1]), int(clipped[2])

    # ---- figures ------------------------------------------------------------------------------

    def draw_brain(self, figure: Figure) -> str:
        """Each run's t map at its cursor; the cursors' text."""
        figure.clear()
        self.axes_owner = {}
        if not self.results:
            figure.text(
                0.5,
                0.5,
                "Run first-level stats to see the maps",
                color="white",
                ha="center",
                va="center",
            )
            return ""
        threshold = self.threshold
        axes_grid = figure.subplots(len(self.results), len(VIEWS), squeeze=False)
        cursor_text = []
        for row, result in enumerate(self.results):
            background, _labels = self.grid_images(result)
            if background is None:
                background = np.where(result.mask, 1.0, 0.0)
            region = self.region_on(result)
            ijk = self.cursor_ijk(row)
            t_here = float(result.t[ijk])
            x, y, z = result.world(ijk)
            cursor_text.append(f"{result.run.label}: MNI {x:.0f}, {y:.0f}, {z:.0f}  t={t_here:.1f}")
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
        figure.subplots_adjust(
            left=0.01, right=0.99, bottom=0.01, top=0.93, wspace=0.02, hspace=0.15
        )
        return "Cursor   " + "   |   ".join(cursor_text)

    def series_for(self, row: int, mode: str) -> tuple[np.ndarray | None, str]:
        result, stats = self.results[row], self.stats[row] if self.stats else None
        if mode == SHOW_REGION:
            return fl.roi_mean_series(result, self.region_on(result)), "region mean"
        if mode == SHOW_PEAK and stats is not None and stats.peak is not None:
            x, y, z = stats.peak_mm or (0, 0, 0)
            return result.voxel_series(stats.peak), f"peak voxel ({x:.0f}, {y:.0f}, {z:.0f})"
        ijk = self.cursor_ijk(row)
        x, y, z = result.world(ijk)
        return result.voxel_series(ijk), f"cursor voxel ({x:.0f}, {y:.0f}, {z:.0f})"

    def draw_time_courses(self, figure: Figure, mode: str) -> None:
        """The signal and the model's fit per run, and the average ON/OFF cycle."""
        figure.clear()
        if not self.results:
            return
        n = len(self.results)
        grid = figure.add_gridspec(n, 2, width_ratios=(3, 1))
        average_axes = figure.add_subplot(grid[:, 1])
        unit = ""
        for row, result in enumerate(self.results):
            color = RUN_COLORS[row % len(RUN_COLORS)]
            axes = figure.add_subplot(grid[row, 0])
            y, where = self.series_for(row, mode)
            for onset, duration in result.design.on_blocks():
                axes.axvspan(onset, onset + duration, color="#ffd54f", alpha=0.3, linewidth=0)
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

    # ---- the comparison table -----------------------------------------------------------------

    def comparison(self) -> list[tuple[str, list[str]]]:
        return fl.comparison_rows(self.results, self.stats) if self.results else []

    def subject(self) -> str:
        return self.results[0].run.subject if self.results else ""

    def csv_name(self) -> str:
        return f"sub-{self.subject()}_{self.setup.key}_first_level.csv"

    def export_csv(self, path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["metric", *[r.run.label for r in self.results]])
            writer.writerows([name, *values] for name, values in self.comparison())

    def save_maps(self, output: Path) -> tuple[int, Path]:
        """(maps written, the folder they are in)"""
        folder = output / "first_level" / f"sub-{self.subject()}" / self.setup.key
        written = [p for r in self.results for p in fl.save_maps(r, folder)]
        return len(written), folder


def differs(name: str, values: list[str]) -> bool:
    """A scan parameter that isn't the same for every run."""
    return name in fl.SCAN_FIELDS and len(set(values)) > 1


def modelled_text(results: list[fl.RunResult]) -> str:
    return f"{len(results)} run(s) modelled: " + ", ".join(r.run.label for r in results)
