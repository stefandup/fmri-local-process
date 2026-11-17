"""How each MRIQC image quality metric (IQM) in the MRIQC tab's table is calculated: MRIQC's
published definition (mriqc.readthedocs.io, 'IQMs', checked against the 24.0.2 source), a plain
explanation, typical values where the literature gives them, and references. Shown by the tab's
Metric formulas dialog.

MRIQC publishes definitions, not pass/fail thresholds: most IQMs depend on the scanner, coil and
sequence, so its authors compare runs within a dataset (as the tab's outlier flag does) or
against the crowdsourced distributions of the MRIQC Web-API.
"""

from fmri_local_process.processing.metric_docs import (
    AFYOUNI_2018,
    KRUGER_2001,
    MURPHY_2007,
    POWER_2012,
    MetricDoc,
    Ref,
)

MRIQC_BOLD_DOCS = Ref(
    "MRIQC documentation: IQMs for functional images",
    "https://mriqc.readthedocs.io/en/24.0.2/iqms/bold.html",
)
MRIQC_T1W_DOCS = Ref(
    "MRIQC documentation: IQMs for structural images",
    "https://mriqc.readthedocs.io/en/24.0.2/iqms/t1w.html",
)
ESTEBAN_2017 = Ref(
    "Esteban O et al. (2017). MRIQC: Advancing the automatic prediction of image quality in MRI "
    "from unseen sites. PLoS ONE 12(9):e0184661.",
    "https://doi.org/10.1371/journal.pone.0184661",
)
ESTEBAN_2019 = Ref(
    "Esteban O et al. (2019). Crowdsourced MRI quality metrics and expert quality annotations "
    "for training of humans and machines. Scientific Data 6:30.",
    "https://doi.org/10.1038/s41597-019-0035-4",
)
POWER_2014 = Ref(
    "Power JD et al. (2014). Methods to detect, characterize, and remove motion artifact in "
    "resting state fMRI. NeuroImage 84:320-341.",
    "https://doi.org/10.1016/j.neuroimage.2013.08.048",
)
PARKES_2018 = Ref(
    "Parkes L et al. (2018). An evaluation of the efficacy, reliability, and sensitivity of "
    "motion correction strategies for resting-state functional MRI. NeuroImage 171:415-436.",
    "https://doi.org/10.1016/j.neuroimage.2017.12.073",
)
NICHOLS_2013 = Ref(
    "Nichols T (2013). Notes on creating a standardized version of DVARS.",
    "https://arxiv.org/abs/1704.01469",
)
GIANNELLI_2010 = Ref(
    "Giannelli M et al. (2010). Characterization of Nyquist ghost in EPI-fMRI acquisition "
    "sequences implemented on two clinical 1.5 T MR scanner systems. J Appl Clin Med Phys "
    "11(4):170-180.",
    "https://doi.org/10.1120/jacmp.v11i4.3237",
)
AFNI_TOUTCOUNT = Ref(
    "AFNI 3dToutcount", "https://afni.nimh.nih.gov/pub/dist/doc/program_help/3dToutcount.html"
)
AFNI_TQUAL = Ref("AFNI 3dTqual", "https://afni.nimh.nih.gov/pub/dist/doc/program_help/3dTqual.html")
AFNI_FWHMX = Ref("AFNI 3dFWHMx", "https://afni.nimh.nih.gov/pub/dist/doc/program_help/3dFWHMx.html")
ATKINSON_1997 = Ref(
    "Atkinson D et al. (1997). Automatic correction of motion artifacts in magnetic resonance "
    "images using an entropy focus criterion. IEEE Trans Med Imaging 16(6):903-910.",
    "https://doi.org/10.1109/42.650886",
)
FRIEDMAN_2008 = Ref(
    "Friedman L et al. (2008). Test-retest and between-site reliability in a multicenter fMRI "
    "study. Hum Brain Mapp 29(8):958-972.",
    "https://doi.org/10.1002/hbm.20440",
)
GANZETTI_2016 = Ref(
    "Ganzetti M, Wenderoth N, Mantini D (2016). Intensity inhomogeneity correction of "
    "structural MR images: a data-driven approach to define input algorithm parameters. "
    "Front Neuroinform 10:10.",
    "https://doi.org/10.3389/fninf.2016.00010",
)
MAGNOTTA_2006 = Ref(
    "Magnotta VA, Friedman L (2006). Measurement of signal-to-noise and contrast-to-noise in "
    "the fBIRN multicenter imaging study. J Digit Imaging 19(2):140-147.",
    "https://doi.org/10.1007/s10278-006-0264-x",
)
SHEHZAD_2015 = Ref(
    "Shehzad Z et al. (2015). The Preprocessed Connectomes Project Quality Assessment Protocol. "
    "Front Neurosci, conference abstract: Neuroinformatics 2015.",
    "https://doi.org/10.3389/conf.fnins.2015.91.00047",
)
MORTAMET_2009 = Ref(
    "Mortamet B et al. (2009). Automatic quality assessment in structural brain magnetic "
    "resonance imaging. Magn Reson Med 62(2):365-372.",
    "https://doi.org/10.1002/mrm.21992",
)
TUSTISON_2010 = Ref(
    "Tustison NJ et al. (2010). N4ITK: improved N3 bias correction. IEEE Trans Med Imaging "
    "29(6):1310-1320.",
    "https://doi.org/10.1109/TMI.2010.2046908",
)

NO_CUTOFF = (
    "No published cut-off: it depends on the scanner, coil and sequence. Compare runs of the "
    "same sequence (the outlier highlight), or with the MRIQC Web-API distributions."
)


def iqm(
    title: str,
    rows: tuple[str, ...],
    equations: tuple[str, ...],
    text: str,
    typical: str = "",
    refs: tuple[Ref, ...] = (),
) -> MetricDoc:
    """A MetricDoc, with the typical values before the references (reads like the page)."""
    return MetricDoc(title, rows, equations, text, refs, typical)


OVERVIEW = iqm(
    "Reading the values",
    (),
    (
        r"\mathrm{outlier:}\ \ x > Q_3 + 1.5\,(Q_3 - Q_1)\ \ \mathrm{or}\ \ "
        r"x < Q_1 - 1.5\,(Q_3 - Q_1)",
    ),
    "MRIQC publishes how each metric is calculated, but no pass/fail thresholds: most "
    "metrics depend on the scanner, coil, voxel size and sequence. So this tab highlights a "
    "value that is an outlier among the runs of the same kind of scan in this dataset "
    "(Tukey's fences above, on the worse side only, with 5 or more runs). Where the "
    "literature gives a rule of thumb, it is under 'Typical values'. MRIQC's own reference "
    "is the crowdsourced distribution of its Web-API (Esteban 2019).",
    "Rules of thumb only. Motion (FD) has the firmest published limits.",
    (ESTEBAN_2017, ESTEBAN_2019),
)

BOLD_DOCS = (
    OVERVIEW,
    iqm(
        "Framewise displacement",
        ("FD mean (mm)", "FD > 0.2 mm (%)"),
        (
            r"\mathrm{FD}_t = |\Delta d_{x,t}| + |\Delta d_{y,t}| + |\Delta d_{z,t}| "
            r"+ |\Delta \alpha_t| + |\Delta \beta_t| + |\Delta \gamma_t|",
            r"\mathrm{FD\ mean} = \dfrac{1}{T}\sum_t \mathrm{FD}_t,\qquad "
            r"\mathrm{FD\%} = 100 \cdot \dfrac{\#\{t : \mathrm{FD}_t > 0.2\,\mathrm{mm}\}}{T + 1}",
        ),
        "Head motion between volumes (Power), from the six realignment parameters: "
        "translations d in mm, rotations turned into mm on a sphere of radius 50 mm. MRIQC "
        "counts the frames above 0.2 mm (and divides by T + 1, the number of volumes).",
        "A frame with FD > 0.2 mm is commonly censored (Power 2014). Parkes 2018 excluded "
        "runs with mean FD > 0.25 mm or more than 20% of frames above 0.2 mm (stringent), or "
        "mean FD > 0.55 mm (lenient). Children and patients move more.",
        (POWER_2012, POWER_2014, PARKES_2018, MRIQC_BOLD_DOCS),
    ),
    iqm(
        "Standardised DVARS",
        ("DVARS (std.)",),
        (
            r"\mathrm{DVARS}_t = \sqrt{\dfrac{1}{N} \sum_{i=1}^{N} (x_{i,t} - x_{i,t-1})^{2}}",
            r"\mathrm{DVARS}^{\mathrm{std}}_t = \dfrac{\mathrm{DVARS}_t}"
            r"{E[\mathrm{DVARS}_t]},\qquad \mathrm{shown:\ its\ mean\ over}\ t",
        ),
        "How much the brain image changes from one volume to the next (N brain voxels, after "
        "motion correction). Standardised: divided by the value expected if the data were "
        "temporally stationary (from the standard deviation of the temporal differences), so "
        "it doesn't depend on the scanner's intensity scale.",
        "About 1 for data without artefacts; a run mean clearly above 1 (e.g. 1.5 or more) "
        "means spikes from motion or scanner artefacts.",
        (NICHOLS_2013, AFYOUNI_2018, MRIQC_BOLD_DOCS),
    ),
    iqm(
        "Temporal SNR",
        ("tSNR",),
        (
            r"\mathrm{tSNR}_v = \dfrac{\langle S_v \rangle_t}{\sigma_{t,v}},\qquad "
            r"\mathrm{tSNR} = \mathrm{median}_{v \in \mathrm{brain}}\ \mathrm{tSNR}_v",
        ),
        "Each voxel's mean over time divided by its standard deviation over time; the median "
        "over the brain mask. It sets how small a signal change can be detected.",
        "Depends strongly on voxel size, field strength and acceleration: roughly 40-100 for "
        "3 mm voxels at 3 T, lower for small voxels or multiband. Compare the same sequence.",
        (KRUGER_2001, MURPHY_2007, MRIQC_BOLD_DOCS),
    ),
    iqm(
        "Spatial SNR",
        ("SNR",),
        (r"\mathrm{SNR} = \dfrac{\mu_F}{\sigma_F \sqrt{n / (n - 1)}}",),
        "On the mean BOLD image: the median brain (foreground, F) intensity over its standard "
        "deviation, n the number of brain voxels.",
        NO_CUTOFF,
        (MAGNOTTA_2006, MRIQC_BOLD_DOCS),
    ),
    iqm(
        "Ghost-to-signal ratio",
        ("Ghost ratio (y)",),
        (r"\mathrm{GSR} = \dfrac{\mu_G - \mu_{NG}}{\mu_S}",),
        "EPI's Nyquist (N/2) ghost: the brain mask shifted by half the field of view along "
        "the phase-encoding axis (y) marks where the ghost falls (G). Its mean intensity, "
        "less the rest of the background (NG), over the brain signal (S).",
        "Close to 0. " + NO_CUTOFF,
        (GIANNELLI_2010, MRIQC_BOLD_DOCS),
    ),
    iqm(
        "AFNI outlier ratio",
        ("Outliers (AFNI)",),
        (
            r"\mathrm{AOR} = \dfrac{1}{T} \sum_t "
            r"\dfrac{\#\{v : |x_{v,t} - m_v| > a\,\mathrm{MAD}_v\}}{V}",
        ),
        "AFNI 3dToutcount: per volume, the fraction of brain voxels far from their own median "
        "over time m (a set from the number of volumes, in MAD units); averaged over volumes.",
        "AFNI's afni_proc commonly censors single volumes with more than 5-10% outliers "
        "(0.05-0.1), so a run mean near that is poor. Clean runs are far lower.",
        (AFNI_TOUTCOUNT, MRIQC_BOLD_DOCS),
    ),
    iqm(
        "AFNI quality index",
        ("Quality index",),
        (
            r"\mathrm{AQI} = \dfrac{1}{T} \sum_t \left(1 - \rho_{\mathrm{Spearman}}(x_t,\ "
            r"\mathrm{median}_t\,x)\right)",
        ),  # fmt: skip
        "AFNI 3dTqual: for each volume, one minus its Spearman rank correlation with the "
        "median volume; averaged over volumes. Volumes unlike the rest (motion, spikes) raise "
        "it.",
        NO_CUTOFF,
        (AFNI_TQUAL, MRIQC_BOLD_DOCS),
    ),
    iqm(
        "Entropy focus criterion (EFC)",
        ("EFC",),
        (
            r"E = -\sum_j \dfrac{x_j}{x_{\max}} \ln \dfrac{x_j}{x_{\max}},\qquad "
            r"x_{\max} = \sqrt{\sum_j x_j^{2}}",
            r"\mathrm{EFC} = \dfrac{E}{-N \frac{1}{\sqrt{N}} \ln \frac{1}{\sqrt{N}}}",
        ),
        "The Shannon entropy of the voxel intensities (N voxels), divided by its maximum so "
        "images of different sizes compare. Ghosting and blurring spread the intensity out, "
        "raising it. On the mean BOLD image here.",
        "Between 0 and 1. " + NO_CUTOFF,
        (ATKINSON_1997, MRIQC_BOLD_DOCS),
    ),
    iqm(
        "Smoothness (FWHM)",
        ("Smoothness (voxels)",),
        (
            r"\mathrm{FWHM}_{\mathrm{avg}} = \dfrac{1}{3}\left(\dfrac{w_x}{\Delta x} "
            r"+ \dfrac{w_y}{\Delta y} + \dfrac{w_z}{\Delta z}\right)",
        ),  # fmt: skip
        "AFNI 3dFWHMx estimates the image's intrinsic smoothness w (mm) along each axis; "
        "MRIQC divides by the voxel size, so the value is in voxels. Higher is blurrier.",
        "Typically between 1 and 3 voxels for unsmoothed data. " + NO_CUTOFF,
        (FRIEDMAN_2008, AFNI_FWHMX, MRIQC_BOLD_DOCS),
    ),
    iqm(
        "Volumes",
        ("Volumes",),
        (),
        "The number of volumes MRIQC read (size_t): a quick check that no run was cut short.",
    ),
)

T1W_DOCS = (
    OVERVIEW,
    iqm(
        "Coefficient of joint variation (CJV)",
        ("CJV",),
        (r"\mathrm{CJV} = \dfrac{\sigma_{WM} + \sigma_{GM}}{|\mu_{WM} - \mu_{GM}|}",),
        "The spread of white- and grey-matter intensities over the gap between them (MRIQC "
        "uses medians and robust spreads, on the bias-corrected image). Noise, intensity "
        "non-uniformity and motion raise it.",
        NO_CUTOFF,
        (GANZETTI_2016, MRIQC_T1W_DOCS),
    ),
    iqm(
        "Contrast-to-noise ratio (CNR)",
        ("CNR",),
        (
            r"\mathrm{CNR} = \dfrac{|\mu_{GM} - \mu_{WM}|}{\sqrt{\sigma_B^{2} + "
            r"\sigma_{WM}^{2} + \sigma_{GM}^{2}}}",
        ),  # fmt: skip
        "The grey/white-matter contrast over the noise: the background (air, B) and the two "
        "tissues' spreads. It measures how well tissues can be told apart.",
        NO_CUTOFF,
        (MAGNOTTA_2006, MRIQC_T1W_DOCS),
    ),
    iqm(
        "Signal-to-noise ratio",
        ("SNR",),
        (
            r"\mathrm{SNR}_k = \dfrac{\mu_k}{\sigma_k \sqrt{n_k / (n_k - 1)}},\qquad "
            r"\mathrm{SNR}_{\mathrm{total}} = \dfrac{1}{3}\left(\mathrm{SNR}_{CSF} "
            r"+ \mathrm{SNR}_{WM} + \mathrm{SNR}_{GM}\right)",
        ),
        "Each tissue's intensity over its own spread (n_k voxels), averaged over CSF, white "
        "and grey matter.",
        NO_CUTOFF,
        (MAGNOTTA_2006, MRIQC_T1W_DOCS),
    ),
    iqm(
        "Entropy focus criterion (EFC)",
        ("EFC",),
        (
            r"E = -\sum_j \dfrac{x_j}{x_{\max}} \ln \dfrac{x_j}{x_{\max}},\qquad "
            r"x_{\max} = \sqrt{\sum_j x_j^{2}}",
            r"\mathrm{EFC} = \dfrac{E}{-N \frac{1}{\sqrt{N}} \ln \frac{1}{\sqrt{N}}}",
        ),
        "The Shannon entropy of the voxel intensities (N voxels), divided by its maximum. "
        "Ghosting and blurring from head motion spread the intensity out, raising it.",
        "Between 0 and 1. " + NO_CUTOFF,
        (ATKINSON_1997, MRIQC_T1W_DOCS),
    ),
    iqm(
        "Foreground-background energy ratio (FBER)",
        ("FBER",),
        (r"\mathrm{FBER} = \dfrac{E[|F|^{2}]}{E[|B|^{2}]}",),
        "The mean energy (squared intensity) inside the head (F) over that of the background "
        "(B). Signal leaking into the background (ghosts, noise) lowers it.",
        NO_CUTOFF,
        (SHEHZAD_2015, MRIQC_T1W_DOCS),
    ),
    iqm(
        "Artefacts in the background (QI1)",
        ("Artefacts (QI1)",),
        (r"\mathrm{QI}_1 = \dfrac{1}{N} \sum_{x \in X_{\mathrm{art}}} 1",),
        "The fraction of the N background (air) voxels detected as artefact (signal structure "
        "where there should only be noise: ghosts, motion, spill-over).",
        "Close to 0. -1 means the background mask was empty.",
        (MORTAMET_2009, MRIQC_T1W_DOCS),
    ),
    iqm(
        "Intensity non-uniformity (INU)",
        ("INU median",),
        (r"\mathrm{INU}_{\mathrm{med}} = \mathrm{median}_{v \in \mathrm{tissue}}\ b_v",),
        "The median of the N4 bias field b (the slow intensity drift across the image that "
        "N4 removes), over the tissue.",
        "1 means no non-uniformity; the further from 1, the stronger the bias (common at "
        "high field and with multichannel head coils).",
        (TUSTISON_2010, MRIQC_T1W_DOCS),
    ),
    iqm(
        "Smoothness (FWHM)",
        ("Smoothness (voxels)",),
        (
            r"\mathrm{FWHM}_{\mathrm{avg}} = \dfrac{1}{3}\left(\dfrac{w_x}{\Delta x} "
            r"+ \dfrac{w_y}{\Delta y} + \dfrac{w_z}{\Delta z}\right)",
        ),  # fmt: skip
        "AFNI 3dFWHMx estimates the image's smoothness w (mm) along each axis; MRIQC divides "
        "by the voxel size, so the value is in voxels. Higher is blurrier (motion blur).",
        NO_CUTOFF,
        (FRIEDMAN_2008, AFNI_FWHMX, MRIQC_T1W_DOCS),
    ),
)

MRIQC_DOCS: dict[str, tuple[MetricDoc, ...]] = {"bold": BOLD_DOCS, "T1w": T1W_DOCS}
