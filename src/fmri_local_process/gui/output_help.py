"""Short 'what is this, what to look for' notes for the output preview tabs (the '?' box).

Plain text, one entry per C-PAC QC figure (keyed by its desc-) and per brain-slice view
(keyed by SliceSpec.title). Rules of thumb only: thresholds differ between studies.
"""

QC_FIGURE_HELP = {
    "framewiseDisplacementJenkinsonPlot": (
        "Head motion from one volume to the next (framewise displacement, Jenkinson "
        "method, in mm).\n\n"
        "Look for: spikes. A mean FD below ~0.2-0.3 mm is good; frames above ~0.5 mm "
        "are commonly censored ('scrubbed'). Many spikes, or a high baseline, can make a "
        "run unusable."
    ),
    "framewiseDisplacementPowerPlot": (
        "Head motion from one volume to the next (framewise displacement, Power method, "
        "in mm).\n\n"
        "Look for: spikes. Frames above ~0.2-0.5 mm are commonly censored; many spikes "
        "can make a run unusable."
    ),
    "movementParametersRot": (
        "Estimated head rotation about the three axes over the run, relative to the "
        "reference volume.\n\n"
        "Look for: sudden jumps (a movement) rather than slow drift. Rotations of more "
        "than ~1-2 degrees, or abrupt steps, deserve a closer look at the FD plot."
    ),
    "movementParametersTrans": (
        "Estimated head translation along the three axes (mm) over the run, relative to "
        "the reference volume.\n\n"
        "Look for: sudden jumps rather than slow drift. Movement larger than about one "
        "voxel (~2-3 mm) is a concern."
    ),
    "boldSnrAxial": (
        "Signal-to-noise map of the BOLD run (mean signal over time divided by its "
        "fluctuation), axial slices.\n\n"
        "Look for: fairly even, bright brain tissue. Dark patches near the sinuses and "
        "ear canals (orbitofrontal cortex, temporal poles) are normal signal dropout; "
        "dropout elsewhere, stripes or a dark edge of the brain point to artefacts or "
        "a too-small field of view."
    ),
    "boldSnrSagittal": (
        "Signal-to-noise map of the BOLD run (mean signal over time divided by its "
        "fluctuation), sagittal slices.\n\n"
        "Look for: fairly even, bright brain tissue. Dropout under the frontal lobe is "
        "normal; a missing top of the head or cerebellum means the field of view cut it "
        "off."
    ),
    "boldSnrHist": (
        "Histogram of the BOLD signal-to-noise values over the brain mask.\n\n"
        "Look for: one clear peak at reasonably high values. A large pile-up near zero "
        "means much of the mask has poor signal (dropout, or the mask includes "
        "non-brain). Compare across subjects: outliers matter more than the exact value."
    ),
}
REG_NOTE = "\n\n'reg-N' marks the result after nuisance-regression strategy N."

SLICE_HELP = {
    "T1w + brain mask": (
        "The preprocessed anatomical scan with the brain mask C-PAC made (skull "
        "stripping) outlined.\n\n"
        "Look for: the outline hugging the brain edge. Skull, scalp, eyes or neck "
        "inside it, or cortex cut off (top of the head, temporal lobes, cerebellum), "
        "means skull stripping failed."
    ),
    "T1w + tissue masks": (
        "The anatomical scan with the grey matter (GM), white matter (WM) and CSF masks "
        "outlined.\n\n"
        "Look for: WM inside, GM following the cortical ribbon around it, CSF in the "
        "ventricles. Skull labelled as tissue, or large missing chunks, means the "
        "segmentation failed."
    ),
    "T1w in template + brain mask": (
        "The anatomical scan after warping to the template (MNI), with the template's "
        "brain outline.\n\n"
        "Look for: the brain filling the outline with the same shape, ventricles and "
        "midline in place. A shifted, stretched or tilted brain means the registration "
        "to the template failed."
    ),
    "Mean BOLD + BOLD mask": (
        "The average functional image over the run, with the functional brain mask "
        "outlined.\n\n"
        "Look for: the mask covering the whole brain and little else. Check that the "
        "field of view includes the top of the head and the cerebellum; distortion "
        "and dropout near the sinuses are normal."
    ),
    "BOLD ref on T1w (coregistration)": (
        "The functional reference image moved into the anatomical (T1w) space, with "
        "the T1w brain outline on top.\n\n"
        "Look for: the BOLD brain sitting inside the outline, edges and ventricles "
        "lining up. A shifted or rotated BOLD means the BOLD-to-T1w registration "
        "failed; small mismatches at the frontal sinuses are normal (distortion)."
    ),
    "BOLD ref in template (normalisation)": (
        "The functional reference image in template (MNI) space, with the template "
        "brain outline on top.\n\n"
        "Look for: the BOLD brain filling the outline with the same shape. A shift, "
        "tilt or stretch means the functional data won't line up across subjects."
    ),
}
SLICE_NOTE = (
    "\n\nSlices run through the brain from one side to the other; left on screen is "
    "the subject's left (neurological view)."
)

MOTION_HELP = (
    "C-PAC's motion summary, one row per run (desc-summary_motion.tsv).\n\n"
    "FD (framewise displacement, mm) is head motion from one volume to the next: the "
    "mean is overall motion, the max the worst jump. DVARS is how much the whole BOLD "
    "signal changes between volumes; spikes that line up with FD spikes confirm motion.\n\n"
    "Look for: runs that stand out from the rest. A common rule of thumb is to "
    "question runs with mean FD above ~0.2-0.5 mm, or with many frames above the FD "
    "threshold. Pick the cut-off for your study before looking at results."
)


def qc_figure_help(desc: str, reg: str) -> str:
    text = QC_FIGURE_HELP.get(desc, "A QC figure C-PAC drew; no notes for this one yet.")
    return text + (REG_NOTE if reg else "")


def slice_help(kind: str) -> str:
    return SLICE_HELP.get(kind, "No notes for this view yet.") + SLICE_NOTE
