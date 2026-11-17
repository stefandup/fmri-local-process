"""How each First-level stats metric is calculated: an equation (matplotlib mathtext, a LaTeX
subset), a plain explanation, and references. Shown by the tab's Metric formulas dialog.

The equations describe what first_level.py computes; the C-PAC QC values are read from C-PAC's
own files, so their equations are the published definitions C-PAC follows.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Ref:
    text: str
    url: str


@dataclass(frozen=True)
class MetricDoc:
    title: str
    rows: tuple[str, ...]  # the comparison-table rows it explains
    equations: tuple[str, ...]  # mathtext, without the $...$
    text: str
    refs: tuple[Ref, ...] = ()
    typical: str = ""  # typical values / published rules of thumb, '' when none given


FRISTON_1998 = Ref(
    "Friston KJ et al. (1998). Event-related fMRI: characterizing differential responses. "
    "NeuroImage 7(1):30-40.",
    "https://doi.org/10.1006/nimg.1997.0306",
)
GLOVER_1999 = Ref(
    "Glover GH (1999). Deconvolution of impulse response in event-related BOLD fMRI. "
    "NeuroImage 9(4):416-429.",
    "https://doi.org/10.1006/nimg.1998.0419",
)
WORSLEY_1995 = Ref(
    "Worsley KJ, Friston KJ (1995). Analysis of fMRI time-series revisited - again. "
    "NeuroImage 2(3):173-181.",
    "https://doi.org/10.1006/nimg.1995.1023",
)
WELVAERT_2013 = Ref(
    "Welvaert M, Rosseel Y (2013). On the definition of signal-to-noise ratio and "
    "contrast-to-noise ratio for fMRI data. PLoS ONE 8(11):e77089.",
    "https://doi.org/10.1371/journal.pone.0077089",
)
MURPHY_2007 = Ref(
    "Murphy K, Bodurka J, Bandettini PA (2007). How long to scan? The relationship between "
    "fMRI temporal signal to noise ratio and necessary scan duration. NeuroImage 34(2):565-574.",
    "https://doi.org/10.1016/j.neuroimage.2006.09.032",
)
DICE_1945 = Ref(
    "Dice LR (1945). Measures of the amount of ecologic association between species. "
    "Ecology 26(3):297-302.",
    "https://doi.org/10.2307/1932409",
)
KRUGER_2001 = Ref(
    "Krüger G, Glover GH (2001). Physiological noise in oxygenation-sensitive magnetic "
    "resonance imaging. Magn Reson Med 46(4):631-637.",
    "https://doi.org/10.1002/mrm.1240",
)
POWER_2012 = Ref(
    "Power JD et al. (2012). Spurious but systematic correlations in functional connectivity "
    "MRI networks arise from subject motion. NeuroImage 59(3):2142-2154.",
    "https://doi.org/10.1016/j.neuroimage.2011.10.018",
)
JENKINSON_2002 = Ref(
    "Jenkinson M et al. (2002). Improved optimization for the robust and accurate linear "
    "registration and motion correction of brain images. NeuroImage 17(2):825-841.",
    "https://doi.org/10.1006/nimg.2002.1132",
)
AFYOUNI_2018 = Ref(
    "Afyouni S, Nichols TE (2018). Insight and inference for DVARS. NeuroImage 172:291-312.",
    "https://doi.org/10.1016/j.neuroimage.2017.12.098",
)
CPAC_DOCS = Ref("C-PAC documentation", "https://fcp-indi.github.io/docs/latest/user/")
PARTIAL_R2 = Ref(
    "Coefficient of partial determination (Wikipedia)",
    "https://en.wikipedia.org/wiki/Coefficient_of_determination"
    "#Coefficient_of_partial_determination",
)
PEARSON = Ref(
    "Pearson correlation coefficient (Wikipedia)",
    "https://en.wikipedia.org/wiki/Pearson_correlation_coefficient",
)

METRIC_DOCS = (
    MetricDoc(
        "The model",
        ("Volumes x TR",),
        (
            r"y = X\beta + \varepsilon,\quad X = [\,x_{\mathrm{task}},\ 1,\ P_1,\ P_2\,]",
            r"h(t) = \dfrac{t^{5}e^{-t}}{\Gamma(6)} "
            r"- \dfrac{1}{6}\,\dfrac{t^{15}e^{-t}}{\Gamma(16)}",
            r"x_{\mathrm{task}} = \dfrac{(b \ast h)(t)}{\max_t\,(b \ast h)(t)}",
        ),
        "Every voxel's time series y (n volumes) is fitted with four columns: the task "
        "regressor, a constant, and linear and quadratic drift (Legendre polynomials P1, P2 "
        "over the run). The task regressor is the ON/OFF boxcar b convolved with SPM's "
        "canonical double-gamma haemodynamic response h (peak about 5 s, undershoot about "
        "15 s), sampled at each volume and scaled so the plateau of a long block is 1. So beta "
        "is the sustained response, in the data's units. The run length is n x TR.",
        (FRISTON_1998, GLOVER_1999),
    ),
    MetricDoc(
        "Fit and t statistic",
        ("Peak t", "Mean t (region)"),
        (
            r"\hat{\beta} = (X^{T}X)^{-1}X^{T}y,\qquad "
            r"\hat{\sigma}^{2} = \dfrac{e^{T}e}{n - p},\quad e = y - X\hat{\beta}",
            r"t = \dfrac{c^{T}\hat{\beta}}{\sqrt{\hat{\sigma}^{2}\,c^{T}(X^{T}X)^{-1}c}},"
            r"\quad c = [1, 0, 0, 0]^{T}",
        ),
        "Ordinary least squares, with p = 4 model columns. t tests the task beta. The peak is "
        "the voxel with the highest t in the ticked regions. C-PAC filtered the data, so the "
        "noise is not white and there are fewer independent time points than n - p: t is "
        "inflated, so compare it between runs processed the same way only.",
        (WORSLEY_1995,),
    ),
    MetricDoc(
        "t per square-root minute",
        ("Peak t per sqrt(min)",),
        (r"\dfrac{t_{\mathrm{peak}}}{\sqrt{n \cdot TR / 60}}",),
        "For a fixed effect size and noise, t grows with the square root of the number of "
        "volumes. Dividing by the square root of the run length in minutes makes runs of "
        "different lengths comparable: detection power per minute of scanning.",
        (MURPHY_2007,),
    ),
    MetricDoc(
        "Beta",
        ("Peak beta",),
        (r"\hat{\beta}_{\mathrm{task}} = \left[(X^{T}X)^{-1}X^{T}y\right]_{1}",),
        "The fitted response amplitude, in the data's units. After C-PAC's nuisance regression "
        "the units are arbitrary, so beta is not comparable between runs: use CNR.",
    ),
    MetricDoc(
        "Contrast-to-noise ratio (CNR)",
        ("Peak CNR", "Mean CNR (region)", "Region-mean CNR"),
        (r"\mathrm{CNR} = \dfrac{\hat{\beta}_{\mathrm{task}}}{\hat{\sigma}}",),
        "The response amplitude over the standard deviation of the residuals: how far the "
        "signal rises above the noise left after the model. It doesn't depend on the run "
        "length or the data's scale, so it is the main number for comparing sequences.",
        (WELVAERT_2013,),
    ),
    MetricDoc(
        "Percent signal change",
        ("Peak % signal change", "Mean % signal change (region)"),
        (
            r"\mathrm{PSC} = 100 \cdot \dfrac{\hat{\beta}_{\mathrm{task}}}{\hat{\beta}_{0}}",
            r"\mathrm{only\ if}\ \ \mathrm{median}_v\,|\bar{y}_v| > "
            r"5 \cdot \mathrm{median}_v\, s_v",
        ),
        "beta_0 is the constant (the voxel mean, as the drifts are Legendre polynomials). "
        "C-PAC's nuisance regression removes voxel means, so this is n/a unless the data "
        "still have them: the check is that the typical voxel mean is much larger than the "
        "typical voxel standard deviation s.",
    ),
    MetricDoc(
        "Partial R squared",
        ("Peak partial R2",),
        (
            r"R^{2}_{\mathrm{partial}} = 1 - "
            r"\dfrac{\mathrm{SS}_{\mathrm{res}}(X)}{\mathrm{SS}_{\mathrm{res}}(X_{\mathrm{drift}})}",
        ),
        "The share of the variance left after the constant and drifts that the task explains: "
        "the full model's residual sum of squares against the model without the task column.",
        (PARTIAL_R2,),
    ),
    MetricDoc(
        "Peak position",
        ("Peak MNI (mm)",),
        (r"[x, y, z, 1]^{T} = A\,[i, j, k, 1]^{T}",),
        "The peak voxel's indices i, j, k mapped to MNI millimetres with the image's affine A.",
    ),
    MetricDoc(
        "Region averages",
        ("Mean t (region)", "Mean CNR (region)", "Mean % signal change (region)"),
        (r"\bar{t}_R = \dfrac{1}{|R|} \sum_{v \in R} t_v",),
        "The average over every voxel v of the ticked regions R (voxels with signal). The same "
        "for CNR and percent signal change.",
    ),
    MetricDoc(
        "Region-mean time series",
        ("Region-mean t", "Region-mean CNR"),
        (r"\bar{y}_R(t) = \dfrac{1}{|R|} \sum_{v \in R} y_v(t)",),
        "The region's voxels averaged into one time series first, then fitted with the same "
        "model. Averaging cancels noise, so this t is usually higher than the mean voxel t.",
    ),
    MetricDoc(
        "Active voxels",
        ("Active voxels",),
        (
            r"A = \{\, v \in R : t_v > t_{\mathrm{thr}} \,\},\qquad "
            r"\mathrm{coverage} = 100 \cdot \dfrac{|A|}{|R|}",
        ),
        "Voxels in the region with t above the threshold (t >) set at the top of the tab.",
    ),
    MetricDoc(
        "Peak distance",
        ("Peak distance to first run (mm)",),
        (r"d = \sqrt{(x_2 - x_1)^{2} + (y_2 - y_1)^{2} + (z_2 - z_1)^{2}}",),
        "Euclidean distance in MNI millimetres between this run's peak and the first run's. "
        "The voxels are 3 mm, so a distance above about 3 mm is a different voxel.",
    ),
    MetricDoc(
        "Dice overlap",
        ("Dice of active voxels vs first run",),
        (r"\mathrm{Dice} = \dfrac{2\,|A_1 \cap A_2|}{|A_1| + |A_2|}",),
        "Overlap of this run's active voxels A2 with the first run's A1: 1 when they are the "
        "same voxels, 0 when none are shared. It depends on the threshold.",
        (DICE_1945,),
    ),
    MetricDoc(
        "t-map correlation",
        ("t-map correlation vs first run",),
        (
            r"r = \dfrac{\sum_{v} (t_{1v} - \bar{t}_1)(t_{2v} - \bar{t}_2)}"
            r"{\sqrt{\sum_{v} (t_{1v} - \bar{t}_1)^{2}}\ \sqrt{\sum_{v} (t_{2v} - \bar{t}_2)^{2}}},"
            r"\quad v \in R_1 \cap R_2",
        ),
        "Pearson correlation of the two runs' t values over the region's voxels: whether the "
        "pattern of activation is the same, whatever the threshold.",
        (PEARSON,),
    ),
    MetricDoc(
        "Average ON/OFF cycle",
        (),
        (
            r"\tilde{y} = y - X_{\mathrm{drift}}\,\hat{\beta}_{\mathrm{drift}},\qquad "
            r"z = \tilde{y} / \hat{\sigma}",
            r"\bar{z}(\phi) = \dfrac{1}{N_\phi} \sum_{t:\ \phi(t) = \phi} z(t),\qquad "
            r"\mathrm{SEM} = \dfrac{s_\phi}{\sqrt{N_\phi}}",
        ),
        "The plot under the time course: the signal with the constant and drifts removed, in "
        "residual-SD units (or in percent of the mean when the data keep it), averaged over "
        "the full cycles at each time point phi into the cycle, with the standard error.",
    ),
    MetricDoc(
        "C-PAC mean BOLD SNR",
        ("C-PAC mean BOLD SNR",),
        (r"\mathrm{tSNR}_v = \dfrac{\bar{y}_v}{s_v}",),
        "Read from C-PAC's own QC file (desc-boldSnr_quality.txt), not computed here. The "
        "standard temporal SNR is a voxel's mean over time divided by its standard deviation "
        "over time, averaged over the brain. See C-PAC's documentation for its exact inputs.",
        (KRUGER_2001, CPAC_DOCS),
    ),
    MetricDoc(
        "Framewise displacement (Power)",
        ("MeanFD_Power",),
        (
            r"\mathrm{FD}_t = |\Delta d_{x,t}| + |\Delta d_{y,t}| + |\Delta d_{z,t}| "
            r"+ r\,(|\Delta \alpha_t| + |\Delta \beta_t| + |\Delta \gamma_t|)",
        ),
        "Head motion between volumes from the six realignment parameters: translations d in mm, "
        "rotations in radians turned into mm on a sphere of radius r = 50 mm. MeanFD_Power is "
        "the mean over the run. Read from C-PAC's motion summary.",
        (POWER_2012, CPAC_DOCS),
    ),
    MetricDoc(
        "Framewise displacement (Jenkinson)",
        ("MeanFD_Jenkinson", "MaxRelRMS", "MaxRelRMSD"),
        (
            r"d_{\mathrm{RMS}} = \sqrt{\dfrac{1}{5}R^{2}\,\mathrm{tr}(A^{T}A) "
            r"+ (t + Ac)^{T}(t + Ac)}",
        ),
        "The root-mean-square movement of the points in a sphere of radius R = 80 mm (centre "
        "c) between volumes, from the relative affine: A is its 3 x 3 part minus the identity, "
        "t its translation. MeanFD_Jenkinson is the mean over the run; MaxRelRMS(D) the "
        "largest such relative movement. Read from C-PAC's motion summary.",
        (JENKINSON_2002, CPAC_DOCS),
    ),
    MetricDoc(
        "DVARS",
        ("MeanDVARS",),
        (r"\mathrm{DVARS}_t = \sqrt{\dfrac{1}{V} \sum_{v=1}^{V} (y_{v,t} - y_{v,t-1})^{2}}",),
        "How much the whole image changes from one volume to the next (V brain voxels): spikes "
        "come with motion. MeanDVARS is the mean over the run. Read from C-PAC's motion "
        "summary.",
        (POWER_2012, AFYOUNI_2018),
    ),
)
