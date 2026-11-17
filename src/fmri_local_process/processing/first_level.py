"""First-level (single run) GLM statistics on C-PAC's preprocessed BOLD in MNI space.

A *setup* says which task it is for and what the design is. The first is the checkerboard
(checkerboard_fmri_task): ON/OFF blocks of a contrast-reversing checkerboard, timed from the
run's first volume: the scanner's dummy scans aren't saved, and the Scan lengths tab trims
the volumes after the task. Its block regressor is convolved with SPM's canonical
haemodynamic response and fitted per voxel by ordinary least squares, next to a constant and
a linear and quadratic drift:

    y = beta * task + b0 + b1 * linear + b2 * quadratic + noise

Per voxel that gives beta (the response amplitude), t (beta over its standard error), CNR
(beta over the residual noise SD: comparable between sequences of different lengths, unlike
t) and the partial R^2 of the task (how much of what the drifts leave the checkerboard explains).
Percent signal change needs the signal's mean, which C-PAC's nuisance regression removes; it
is only given when the data still have it.

C-PAC's 'preproc' pipeline already regressed motion, CSF and aCompCor out (reg-2; reg-1 also
the global signal) and band-passed 0.01-0.1 Hz, which keeps the 40 s block cycle (0.025 Hz).
The filtering leaves fewer independent time points than volumes, so t values are optimistic;
compare them between runs processed the same way, not against textbook thresholds.
"""

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import nibabel as nib
import numpy as np
from nibabel.filebasedimages import ImageFileError
from scipy.stats import gamma

from fmri_local_process.processing.cpac_outputs import bids_entities

HRF_DT_S = 0.1  # resolution the block regressor is built and convolved at
HRF_LENGTH_S = 32.0
DEFAULT_T_THRESHOLD = 3.1
MNI_BOLD_PATTERN = "*_space-MNI152NLin6ASym_reg-{reg}_desc-preproc_bold.nii.gz"
# reg-N: C-PAC's preproc pipeline's nuisance regression strategies (see module docstring).
DATA_CHOICES = {
    "2": "reg-2: motion, CSF, aCompCor regressed (no global signal) - recommended",
    "1": "reg-1: as reg-2, plus global signal regression",
}
# BIDS sidecar fields worth comparing between sequences, in display order.
SCAN_FIELDS = (
    "ProtocolName",
    "SeriesDescription",
    "RepetitionTime",
    "EchoTime",
    "FlipAngle",
    "MultibandAccelerationFactor",
    "ParallelReductionFactorInPlane",
    "SliceThickness",
    "PhaseEncodingDirection",
    "EffectiveEchoSpacing",
    "TotalReadoutTime",
    "PixelBandwidth",
    "ReceiveCoilName",
    "AcquisitionTime",
)
MOTION_FIELDS = ("MeanFD_Power", "MeanFD_Jenkinson", "MeanDVARS", "MaxRelRMS", "MaxRelRMSD")


class FirstLevelError(RuntimeError):
    """A run could not be read or modelled."""


# ---- setups -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockDesign:
    """Alternating ON/OFF blocks, as checkerboard_fmri_task's [paradigm] and [scanner]."""

    first_block: str = "off"  # "on" or "off"
    on_s: float = 20.0
    off_s: float = 20.0
    total_s: float = 200.0

    @property
    def cycle_s(self) -> float:
        return self.on_s + self.off_s

    def run_volumes(self, tr: float) -> int:
        """Volumes a run should have at this TR. The task starts on the first volume (the
        scanner's dummy scans come before its first trigger and aren't saved)."""
        return math.ceil(self.total_s / tr - 1e-6)

    def on_blocks(self) -> list[tuple[float, float]]:
        """(onset, duration) of each ON block, in seconds from the task start."""
        blocks, onset, condition = [], 0.0, self.first_block
        while onset < self.total_s - 1e-9:
            planned = self.on_s if condition == "on" else self.off_s
            duration = min(planned, self.total_s - onset)
            if condition == "on":
                blocks.append((onset, duration))
            onset += duration
            condition = "off" if condition == "on" else "on"
        return blocks


@dataclass(frozen=True)
class FirstLevelSetup:
    key: str
    name: str
    task: str  # BIDS task label
    design: BlockDesign
    focus: dict[str, tuple[str, ...]]  # atlas key -> region-name keywords to tick first
    about: str = ""


CHECKERBOARD = FirstLevelSetup(
    key="checkerboard",
    name="Checkerboard: ON/OFF visual blocks",
    task="Checkerboard",
    design=BlockDesign(),
    focus={
        # Occipital lobe only: plain "Occipital" would add the temporo-occipital regions.
        "ho-cort": (
            "Occipital Pole",
            "Lateral Occipital",
            "Occipital Fusiform Gyrus",
            "Intracalcarine",
            "Supracalcarine",
            "Cuneal",
            "Lingual",
        ),
        "mni-lobes": ("Occipital",),
        "juelich": ("Visual cortex",),
    },
    about=(
        "checkerboard_fmri_task defaults: OFF first, 20 s ON / 20 s OFF, 200 s, task "
        "starting on the run's first volume (the scanner's dummy scans aren't saved; trim "
        "the volumes after the task on the Scan lengths tab). Change the design below if a "
        "session ran another task_config.toml."
    ),
)
SETUPS = (CHECKERBOARD,)


# ---- design matrix ----------------------------------------------------------------------------


def spm_hrf(dt: float = HRF_DT_S, length_s: float = HRF_LENGTH_S) -> np.ndarray:
    """SPM's canonical double-gamma HRF (peak ~5 s, undershoot ~15 s), summing to 1."""
    t = np.arange(0.0, length_s, dt)
    hrf = gamma.pdf(t, 6) - gamma.pdf(t, 16) / 6.0
    return hrf / hrf.sum()


def task_regressor(design: BlockDesign, n_volumes: int, tr: float) -> np.ndarray:
    """The ON blocks convolved with the HRF, sampled at each volume, plateau scaled to 1 (so
    beta is the sustained response in the data's units)."""
    n_fine = int(np.ceil((n_volumes * tr + HRF_LENGTH_S) / HRF_DT_S))
    fine_t = np.arange(n_fine) * HRF_DT_S
    boxcar = np.zeros(n_fine)
    for onset, duration in design.on_blocks():
        boxcar[(fine_t >= onset) & (fine_t < onset + duration)] = 1.0
    response = np.convolve(boxcar, spm_hrf())[:n_fine]
    sampled = np.interp(np.arange(n_volumes) * tr, fine_t, response)
    peak = sampled.max()
    return sampled / peak if peak > 0 else sampled


def design_matrix(task: np.ndarray) -> np.ndarray:
    """Columns: task, constant, linear and quadratic drift (Legendre, so b0 is the mean)."""
    x = np.linspace(-1.0, 1.0, task.size)
    return np.column_stack([task, np.ones_like(x), x, 1.5 * x**2 - 0.5])


# ---- the fit ----------------------------------------------------------------------------------


@dataclass
class GlmFit:
    beta: np.ndarray  # (columns, voxels)
    t: np.ndarray  # task t per voxel
    residual_sd: np.ndarray
    partial_r2: np.ndarray
    dof: int


def fit_glm(y: np.ndarray, x: np.ndarray) -> GlmFit:
    """OLS of y (time, voxels) on x (time, columns); statistics are for column 0 (task)."""
    pinv = np.linalg.pinv(x)
    beta = pinv @ y
    residual = y - x @ beta
    dof = x.shape[0] - np.linalg.matrix_rank(x)
    if dof <= 0:
        raise FirstLevelError("More model columns than volumes")
    ss_res = (residual**2).sum(axis=0)
    sigma2 = ss_res / dof
    var_task = (pinv @ pinv.T)[0, 0]  # c (X'X)^-1 c' for c = [1, 0, 0, 0]
    with np.errstate(divide="ignore", invalid="ignore"):
        t = beta[0] / np.sqrt(sigma2 * var_task)
        reduced = x[:, 1:]
        reduced_res = y - reduced @ (np.linalg.pinv(reduced) @ y)
        ss_reduced = (reduced_res**2).sum(axis=0)
        partial_r2 = 1.0 - ss_res / ss_reduced
    return GlmFit(beta, np.nan_to_num(t), np.sqrt(sigma2), np.nan_to_num(partial_r2), int(dof))


# ---- runs -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunFile:
    path: Path
    subject: str
    session: str
    run: str  # "" when the BIDS name has no run entity
    task: str

    @property
    def label(self) -> str:
        return f"ses-{self.session}" + (f" run-{self.run}" if self.run else "")

    @property
    def prefix(self) -> str:
        """'sub-1_ses-2_task-Checkerboard[_run-1]': the start of C-PAC's names for this run."""
        return self.path.name.split("_space-", 1)[0]


def find_runs(output_root: Path, subject: str, task: str, reg: str = "2") -> list[RunFile]:
    """This subject's preprocessed MNI-space BOLD runs of task, in session/run order."""
    pattern = MNI_BOLD_PATTERN.format(reg=reg)
    found = sorted((output_root / "output").glob(f"pipeline_*/sub-{subject}/ses-*/func/{pattern}"))
    runs = []
    for path in found:
        entities = bids_entities(path.name)
        if entities.get("task") != task:
            continue
        runs.append(RunFile(path, subject, entities.get("ses", "?"), entities.get("run", ""), task))
    return sorted(runs, key=lambda r: (r.session.zfill(4), r.run.zfill(4)))


def output_subjects(output_root: Path, task: str | None = None) -> list[str]:
    """Subjects with at least one preprocessed MNI-space BOLD (of task) in output_root."""
    subjects = set()
    for path in (output_root / "output").glob(
        "pipeline_*/sub-*/ses-*/func/*_space-MNI152NLin6ASym_reg-*_desc-preproc_bold.nii.gz"
    ):
        entities = bids_entities(path.name)
        if task is None or entities.get("task") == task:
            subjects.add(entities.get("sub", path.parts[-4].removeprefix("sub-")))
    return sorted(subjects)


@dataclass
class RunResult:
    run: RunFile
    tr: float
    n_volumes: int
    affine: np.ndarray
    mask: np.ndarray  # 3D bool: voxels with signal
    index: np.ndarray  # 3D int: row in series, -1 outside the mask
    series: np.ndarray  # (voxels, time) float32
    x: np.ndarray  # design matrix (time, columns)
    design: BlockDesign
    beta: np.ndarray  # 3D task beta (0 outside the mask)
    t: np.ndarray
    cnr: np.ndarray
    partial_r2: np.ndarray
    psc: np.ndarray | None  # 3D percent signal change, None when the mean was removed
    dof: int
    extra: dict[str, str] = field(default_factory=dict)  # scan params, C-PAC QC values

    @property
    def minutes(self) -> float:
        return self.n_volumes * self.tr / 60.0

    def voxel_series(self, ijk: tuple[int, int, int]) -> np.ndarray | None:
        row = int(self.index[ijk])
        return None if row < 0 else self.series[row].astype(np.float64)

    def world(self, ijk: tuple[int, int, int]) -> tuple[float, float, float]:
        x, y, z = self.affine[:3, :3] @ np.asarray(ijk, float) + self.affine[:3, 3]
        return float(x), float(y), float(z)


def has_baseline(series: np.ndarray) -> bool:
    """True when voxel means are still in the data (raw BOLD: mean >> SD); C-PAC's nuisance
    regression leaves them near zero, and percent signal change is then meaningless."""
    if not series.size:
        return False
    means = np.abs(series.mean(axis=1))
    sds = series.std(axis=1)
    return bool(np.median(means) > 5.0 * np.median(sds))


def load_run(path: Path) -> tuple[np.ndarray, np.ndarray, float]:
    """A 4D BOLD as (data float32, affine, TR in s), reoriented to RAS+."""
    try:
        image = nib.as_closest_canonical(nib.load(path))
        data = np.asarray(image.dataobj, dtype=np.float32)
    except (OSError, EOFError, ImageFileError) as e:
        raise FirstLevelError(f"Can't read {path.name}: {e}") from e
    if data.ndim != 4:
        raise FirstLevelError(f"{path.name} is not a 4D series")
    tr = float(image.header.get_zooms()[3])
    return data, np.asarray(image.affine), tr


def model_run(run: RunFile, design: BlockDesign, tr_override: float | None = None) -> RunResult:
    """Fit the design to every voxel of one run."""
    data, affine, header_tr = load_run(run.path)
    tr = tr_override or header_tr
    if tr <= 0:
        raise FirstLevelError(f"{run.path.name}: no repetition time in the header")
    n_volumes = data.shape[3]
    if design.total_s > n_volumes * tr + 1e-6:
        raise FirstLevelError(
            f"{run.label}: {n_volumes} volumes x {tr:.3f} s is shorter than the design "
            f"({design.total_s:g} s)"
        )
    mask = np.isfinite(data).all(axis=3) & (data.std(axis=3) > 0)
    series = data[mask]  # (voxels, time)
    del data
    index = np.full(mask.shape, -1, dtype=np.int64)
    index[mask] = np.arange(series.shape[0])
    x = design_matrix(task_regressor(design, n_volumes, tr))
    fit = fit_glm(series.T.astype(np.float64), x)

    def volume(values: np.ndarray) -> np.ndarray:
        out = np.zeros(mask.shape, dtype=np.float32)
        out[mask] = values
        return out

    psc = None
    if has_baseline(series):
        with np.errstate(divide="ignore", invalid="ignore"):
            psc = volume(np.nan_to_num(100.0 * fit.beta[0] / fit.beta[1]))
    with np.errstate(divide="ignore", invalid="ignore"):
        cnr = np.nan_to_num(fit.beta[0] / fit.residual_sd)
    return RunResult(
        run=run,
        tr=tr,
        n_volumes=n_volumes,
        affine=affine,
        mask=mask,
        index=index,
        series=series,
        x=x,
        design=design,
        beta=volume(fit.beta[0]),
        t=volume(fit.t),
        cnr=volume(cnr),
        partial_r2=volume(fit.partial_r2),
        psc=psc,
        dof=fit.dof,
    )


def fitted(result: RunResult, y: np.ndarray) -> np.ndarray:
    """The model's fit to one time series (all columns: task plus drifts)."""
    return result.x @ (np.linalg.pinv(result.x) @ y)


# ---- regions ----------------------------------------------------------------------------------


@dataclass
class RoiStats:
    n_voxels: int
    peak: tuple[int, int, int] | None
    peak_mm: tuple[float, float, float] | None
    peak_t: float
    peak_beta: float
    peak_cnr: float
    peak_psc: float | None
    peak_r2: float
    mean_t: float
    mean_beta: float
    mean_cnr: float
    mean_psc: float | None
    n_active: int
    roi_t: float  # GLM t of the region's mean time series
    roi_cnr: float
    roi_psc: float | None
    active: np.ndarray = field(repr=False)  # 3D bool: t above threshold in the region
    region: np.ndarray = field(repr=False)  # 3D bool: the region voxels with signal


def roi_mean_series(result: RunResult, roi: np.ndarray) -> np.ndarray | None:
    rows = result.index[roi & result.mask]
    return result.series[rows].mean(axis=0).astype(np.float64) if rows.size else None


def roi_stats(result: RunResult, roi: np.ndarray, threshold: float) -> RoiStats:
    """Peak voxel (highest t), averages and active extent within roi (on the run's grid)."""
    inside = roi & result.mask
    n = int(inside.sum())
    empty = np.zeros(roi.shape, bool)
    if not n:
        return RoiStats(0, None, None, 0, 0, 0, None, 0, 0, 0, 0, None, 0, 0, 0, None, empty, empty)
    t_inside = np.where(inside, result.t, -np.inf)
    flat = int(np.argmax(t_inside))
    peak = tuple(int(i) for i in np.unravel_index(flat, roi.shape))
    peak3 = (peak[0], peak[1], peak[2])
    active = inside & (result.t > threshold)
    mean_series = roi_mean_series(result, roi)
    roi_t = roi_cnr = 0.0
    roi_psc = None
    if mean_series is not None:
        fit = fit_glm(mean_series[:, None], result.x)
        roi_t = float(fit.t[0])
        roi_cnr = float(fit.beta[0, 0] / fit.residual_sd[0]) if fit.residual_sd[0] > 0 else 0.0
        if result.psc is not None and fit.beta[1, 0] != 0:
            roi_psc = float(100.0 * fit.beta[0, 0] / fit.beta[1, 0])
    return RoiStats(
        n_voxels=n,
        peak=peak3,
        peak_mm=result.world(peak3),
        peak_t=float(result.t[peak3]),
        peak_beta=float(result.beta[peak3]),
        peak_cnr=float(result.cnr[peak3]),
        peak_psc=None if result.psc is None else float(result.psc[peak3]),
        peak_r2=float(result.partial_r2[peak3]),
        mean_t=float(result.t[inside].mean()),
        mean_beta=float(result.beta[inside].mean()),
        mean_cnr=float(result.cnr[inside].mean()),
        mean_psc=None if result.psc is None else float(result.psc[inside].mean()),
        n_active=int(active.sum()),
        roi_t=roi_t,
        roi_cnr=roi_cnr,
        roi_psc=roi_psc,
        active=active,
        region=inside,
    )


def block_average(
    result: RunResult, y: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """The response averaged over the design's full ON/OFF cycles, after the drifts are taken
    out: (seconds into the cycle, mean, SEM, unit). In % of the mean when the data have one,
    else in residual-SD units (z), so runs with different scaling still overlay."""
    full = fit_glm(y[:, None], result.x)
    detrended = y - result.x[:, 1:] @ full.beta[1:, 0]  # OFF blocks sit at 0
    if result.psc is not None and full.beta[1, 0] != 0:
        scaled, unit = 100.0 * detrended / full.beta[1, 0], "% signal change"
    else:
        sd = full.residual_sd[0]
        scaled, unit = (detrended / sd if sd > 0 else detrended), "residual SD (z)"
    design = result.design
    task_t = np.arange(y.size) * result.tr
    n_cycles = int(design.total_s // design.cycle_s)
    edges = np.arange(0.0, design.cycle_s + result.tr / 2, result.tr)
    centres = edges[:-1] + result.tr / 2
    means, sems = np.full(centres.size, np.nan), np.full(centres.size, np.nan)
    if n_cycles:
        inside = (task_t >= 0) & (task_t < n_cycles * design.cycle_s)
        phase = np.mod(task_t[inside], design.cycle_s)
        values = scaled[inside]
        which = np.clip(np.digitize(phase, edges) - 1, 0, centres.size - 1)
        for b in range(centres.size):
            in_bin = values[which == b]
            if in_bin.size:
                means[b] = in_bin.mean()
                sems[b] = in_bin.std(ddof=1) / np.sqrt(in_bin.size) if in_bin.size > 1 else 0
    return centres, means, sems, unit


# ---- run details for the comparison -----------------------------------------------------------


def bids_sidecar(bids_root: Path | None, run: RunFile) -> Path | None:
    if bids_root is None:
        return None
    name = f"sub-{run.subject}_ses-{run.session}_task-{run.task}"
    name += f"_run-{run.run}_bold.json" if run.run else "_bold.json"
    path = bids_root / f"sub-{run.subject}" / f"ses-{run.session}" / "func" / name
    return path if path.is_file() else None


def scan_details(bids_root: Path | None, run: RunFile) -> dict[str, str]:
    """The sidecar fields in SCAN_FIELDS that the run's BIDS JSON has."""
    sidecar = bids_sidecar(bids_root, run)
    if sidecar is None:
        return {}
    try:
        info = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {key: str(info[key]) for key in SCAN_FIELDS if key in info}


def cpac_qc_values(run: RunFile, reg: str) -> dict[str, str]:
    """C-PAC's mean BOLD SNR (of reg) and motion summary values for the run."""
    folder = run.path.parent
    values: dict[str, str] = {}
    snr = folder / f"{run.prefix}_reg-{reg}_desc-boldSnr_quality.txt"
    try:
        values["C-PAC mean BOLD SNR"] = f"{float(snr.read_text().split()[0]):.1f}"
    except (OSError, ValueError, IndexError):
        pass
    motion = folder / f"{run.prefix}_desc-summary_motion.tsv"
    try:
        with motion.open(encoding="utf-8", newline="") as handle:
            rows = [row for row in csv.reader(handle, delimiter="\t") if row]
    except OSError:
        rows = []
    if len(rows) >= 2:
        header, first = rows[0], rows[1]
        for key in MOTION_FIELDS:
            if key in header and header.index(key) < len(first):
                try:
                    values[key] = f"{float(first[header.index(key)]):.3f}"
                except ValueError:
                    values[key] = first[header.index(key)]
    return values


# ---- comparing runs ---------------------------------------------------------------------------


def dice(a: np.ndarray, b: np.ndarray) -> float | None:
    total = int(a.sum() + b.sum())
    return None if total == 0 else 2.0 * int((a & b).sum()) / total


def map_correlation(a: np.ndarray, b: np.ndarray, where: np.ndarray) -> float | None:
    if where.sum() < 3:
        return None
    x, y = a[where], b[where]
    if x.std() == 0 or y.std() == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def fmt(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


METRIC_HELP = {
    "Volumes x TR": "Length of the run as modelled.",
    "Peak t": "Highest t in the ticked regions. Grows with run length (sqrt of time).",
    "Peak t per sqrt(min)": "Peak t / sqrt(run minutes): detection power per scan time, fair "
    "between sequences of different lengths.",
    "Peak beta": "Response amplitude at the peak, in the data's units.",
    "Peak CNR": "Peak beta / residual noise SD: contrast-to-noise, independent of run length.",
    "Peak % signal change": "100 x beta / mean at the peak (only when the data keep their mean).",
    "Peak partial R2": "Share of the drift-removed variance the task explains at the peak.",
    "Peak MNI (mm)": "Where the peak is.",
    "Mean t (region)": "Average t over all ticked-region voxels.",
    "Mean CNR (region)": "Average CNR over all ticked-region voxels.",
    "Mean % signal change (region)": "Average % signal change over the region.",
    "Region-mean t": "t of the region's average time series: the region as one signal.",
    "Region-mean CNR": "CNR of the region's average time series.",
    "Active voxels": "Voxels in the region with t above the threshold (and % of the region).",
    "Peak distance to first run (mm)": "How far this run's peak is from the first run's.",
    "Dice of active voxels vs first run": "Overlap of the active voxels with the first run's "
    "(1 = same voxels, 0 = none shared).",
    "t-map correlation vs first run": "Spatial correlation of the t maps within the region.",
}


def comparison_rows(results: list[RunResult], stats: list[RoiStats]) -> list[tuple[str, list[str]]]:
    """(metric, value per run) rows for the side-by-side table."""
    rows: list[tuple[str, list[str]]] = []

    def add(name: str, values: list[str]) -> None:
        rows.append((name, values))

    add("Volumes x TR", [f"{r.n_volumes} x {r.tr:.3f} s ({r.minutes:.1f} min)" for r in results])
    add("Peak t", [fmt(s.peak_t) for s in stats])
    add(
        "Peak t per sqrt(min)",
        [fmt(s.peak_t / np.sqrt(r.minutes)) for r, s in zip(results, stats, strict=True)],
    )
    add("Peak beta", [fmt(s.peak_beta, 3) for s in stats])
    add("Peak CNR", [fmt(s.peak_cnr) for s in stats])
    add("Peak % signal change", [fmt(s.peak_psc) for s in stats])
    add("Peak partial R2", [fmt(s.peak_r2) for s in stats])
    add(
        "Peak MNI (mm)",
        [
            "n/a" if s.peak_mm is None else "{:.0f}, {:.0f}, {:.0f}".format(*s.peak_mm)
            for s in stats
        ],
    )
    add("Mean t (region)", [fmt(s.mean_t) for s in stats])
    add("Mean CNR (region)", [fmt(s.mean_cnr) for s in stats])
    add("Mean % signal change (region)", [fmt(s.mean_psc) for s in stats])
    add("Region-mean t", [fmt(s.roi_t) for s in stats])
    add("Region-mean CNR", [fmt(s.roi_cnr) for s in stats])
    add(
        "Active voxels",
        [
            f"{s.n_active} / {s.n_voxels} ({100 * s.n_active / s.n_voxels:.0f}%)"
            if s.n_voxels
            else "n/a"
            for s in stats
        ],
    )
    if len(results) > 1:
        first, first_stats = results[0], stats[0]
        distance, overlap, correlation = ["-"], ["-"], ["-"]
        for result, stat in zip(results[1:], stats[1:], strict=True):
            if stat.peak_mm and first_stats.peak_mm:
                gap = np.linalg.norm(np.subtract(stat.peak_mm, first_stats.peak_mm))
                distance.append(f"{gap:.1f}")
            else:
                distance.append("n/a")
            same_grid = result.t.shape == first.t.shape and np.allclose(result.affine, first.affine)
            if same_grid:
                overlap.append(fmt(dice(stat.active, first_stats.active)))
                region = stat.region & first_stats.region
                correlation.append(fmt(map_correlation(result.t, first.t, region)))
            else:
                overlap.append("n/a (grids differ)")
                correlation.append("n/a (grids differ)")
        add("Peak distance to first run (mm)", distance)
        add("Dice of active voxels vs first run", overlap)
        add("t-map correlation vs first run", correlation)
    keys: list[str] = []
    for result in results:
        keys += [k for k in result.extra if k not in keys]
    for key in keys:
        add(key, [r.extra.get(key, "") for r in results])
    return rows


def save_maps(result: RunResult, folder: Path) -> list[Path]:
    """The run's beta, t, CNR (and % signal change) maps as NIfTIs in folder."""
    folder.mkdir(parents=True, exist_ok=True)
    maps = {"beta": result.beta, "tstat": result.t, "cnr": result.cnr}
    if result.psc is not None:
        maps["psc"] = result.psc
    written = []
    for name, data in maps.items():
        path = folder / f"{result.run.prefix}_space-MNI152NLin6ASym_stat-{name}_statmap.nii.gz"
        nib.save(nib.Nifti1Image(data.astype(np.float32), result.affine), path)
        written.append(path)
    return written
