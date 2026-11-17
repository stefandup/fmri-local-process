"""Standard brain atlases for region masks, taken from FSL inside the C-PAC Docker image.

The C-PAC image ships FSL's atlases in MNI152 space (the same template C-PAC normalises the
BOLD to, MNI152NLin6ASym), so the region labels line up with C-PAC's space-MNI outputs as they
are. They are copied out once into a local cache folder (fetch_atlases) and read from there:
no internet download, and nothing is copied into this repository.

Harvard-Oxford's cortical atlas has one label per region for both hemispheres; here it is
split into left and right (x < 0 is left in MNI), like the AAL regions.
"""

import os
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import nibabel as nib
import numpy as np
from nibabel.filebasedimages import ImageFileError
from scipy.ndimage import map_coordinates

from fmri_local_process.processing.review_mode import atlas_dir

FSL_ATLASES = "/usr/share/fsl/6.0/data/atlases"
FSL_STANDARD = "/usr/share/fsl/6.0/data/standard"
TEMPLATE_FILE = "MNI152_T1_2mm_brain.nii.gz"
CACHE_MOUNT = "/atlas_cache"
HEMISPHERE_OFFSET = 1000  # right-hemisphere copy of a bilateral label: value + this


@dataclass(frozen=True)
class AtlasSpec:
    key: str
    name: str
    image: str  # relative to FSL_ATLASES
    labels_xml: str  # relative to FSL_ATLASES
    split_hemispheres: bool = False

    @property
    def image_file(self) -> str:
        return Path(self.image).name

    @property
    def xml_file(self) -> str:
        return Path(self.labels_xml).name


ATLASES = (
    AtlasSpec(
        "ho-cort",
        "Harvard-Oxford cortical (48 regions x L/R)",
        "HarvardOxford/HarvardOxford-cort-maxprob-thr25-2mm.nii.gz",
        "HarvardOxford-Cortical.xml",
        split_hemispheres=True,
    ),
    AtlasSpec(
        "ho-sub",
        "Harvard-Oxford subcortical",
        "HarvardOxford/HarvardOxford-sub-maxprob-thr25-2mm.nii.gz",
        "HarvardOxford-Subcortical.xml",
    ),
    AtlasSpec(
        "juelich",
        "Juelich histological (V1, V2, ... L/R)",
        "Juelich/Juelich-maxprob-thr25-2mm.nii.gz",
        "Juelich.xml",
    ),
    AtlasSpec(
        "mni-lobes",
        "MNI structural (lobes)",
        "MNI/MNI-maxprob-thr25-2mm.nii.gz",
        "MNI.xml",
        split_hemispheres=True,
    ),
)
ATLAS_BY_KEY = {spec.key: spec for spec in ATLASES}


def default_cache() -> Path:
    """Per-user cache for the copied atlases (outside the repo and the data folders); in a
    review package, the atlases packed with it."""
    if (packaged := atlas_dir()) is not None:
        return packaged
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "fmri_local_process" / "atlases"


class AtlasError(RuntimeError):
    """The atlases could not be fetched or read."""


@dataclass(frozen=True)
class Region:
    value: int  # label value in Atlas.data
    name: str


@dataclass
class Atlas:
    spec: AtlasSpec
    data: np.ndarray  # 3D integer labels, 0 = no region
    affine: np.ndarray
    regions: list[Region]

    def region_mask(self, values: set[int]) -> np.ndarray:
        return np.isin(self.data, list(values)) if values else np.zeros(self.data.shape, bool)


def cache_files() -> list[str]:
    """Every file the cache needs: each atlas image and label list, and the template."""
    names = [TEMPLATE_FILE]
    for spec in ATLASES:
        names += [spec.image_file, spec.xml_file]
    return names


def atlases_present(cache: Path) -> bool:
    return all((cache / name).is_file() for name in cache_files())


def fetch_script() -> str:
    """bash for the C-PAC image: copy the atlas files flat into the mounted cache."""
    sources = [f"{FSL_STANDARD}/{TEMPLATE_FILE}"]
    for spec in ATLASES:
        sources += [f"{FSL_ATLASES}/{spec.image}", f"{FSL_ATLASES}/{spec.labels_xml}"]
    return "set -e; cp " + " ".join(sources) + f" {CACHE_MOUNT}/"


def fetch_command(docker: Path, image: str, cache: Path) -> list[str]:
    return [
        str(docker),
        "run",
        "--rm",
        "--entrypoint",
        "bash",
        "-v",
        f"{cache.resolve()}:{CACHE_MOUNT}",
        image,
        "-c",
        fetch_script(),
    ]


def fetch_atlases(docker: Path, image: str, cache: Path) -> None:
    """Copy FSL's atlases and the MNI152 template out of the C-PAC image into cache."""
    cache.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(fetch_command(docker, image, cache), capture_output=True, text=True)
    if result.returncode != 0 or not atlases_present(cache):
        raise AtlasError(f"Could not copy the atlases out of {image}: {result.stderr.strip()}")


def read_labels(xml_file: Path) -> dict[int, str]:
    """FSL atlas labels as {image value: name}; FSL's index is 0-based, image values 1-based."""
    try:
        root = ET.parse(xml_file).getroot()
    except (OSError, ET.ParseError) as e:
        raise AtlasError(f"Can't read the labels in {xml_file}: {e}") from e
    return {
        int(label.get("index", "-1")) + 1: (label.text or "").strip()
        for label in root.iter("label")
    }


def split_hemispheres(
    data: np.ndarray, affine: np.ndarray, labels: dict[int, str]
) -> tuple[np.ndarray, list[Region]]:
    """Bilateral labels as separate left (value) and right (value + offset) regions."""
    x_index = np.arange(data.shape[0])
    x_world = affine[0, 0] * x_index + affine[0, 3]  # FSL atlases are not rotated
    right = (x_world >= 0)[:, None, None] & (data > 0)
    split = np.where(right, data + HEMISPHERE_OFFSET, data)
    regions = []
    for value, name in labels.items():
        regions.append(Region(value, f"{name} (L)"))
        regions.append(Region(value + HEMISPHERE_OFFSET, f"{name} (R)"))
    return split, regions


def load_atlas(cache: Path, key: str) -> Atlas:
    spec = ATLAS_BY_KEY[key]
    try:
        image = nib.as_closest_canonical(nib.load(cache / spec.image_file))
    except (OSError, ImageFileError) as e:
        raise AtlasError(f"Can't read {spec.image_file}: {e}") from e
    data = np.asarray(image.dataobj).astype(np.int32)
    affine = np.asarray(image.affine)
    labels = read_labels(cache / spec.xml_file)
    if spec.split_hemispheres:
        data, regions = split_hemispheres(data, affine, labels)
    else:
        regions = [Region(value, name) for value, name in labels.items()]
    present = set(np.unique(data).tolist())
    regions = [region for region in regions if region.value in present]
    return Atlas(spec, data, affine, regions)


def load_template(cache: Path) -> tuple[np.ndarray, np.ndarray]:
    """The MNI152 2 mm brain (background for the slice views): (data, affine), RAS+."""
    image = nib.as_closest_canonical(nib.load(cache / TEMPLATE_FILE))
    return np.asarray(image.dataobj, dtype=np.float32), np.asarray(image.affine)


def resample_nearest(
    data: np.ndarray, affine: np.ndarray, shape: tuple[int, ...], target_affine: np.ndarray
) -> np.ndarray:
    """data sampled on another grid (target shape and affine) by nearest neighbour, through
    world (mm) coordinates: atlas labels on a 3 mm BOLD grid, or BOLD maps on the template."""
    ijk = np.indices(shape[:3]).reshape(3, -1)
    world = target_affine[:3, :3] @ ijk + target_affine[:3, 3:]
    to_source = np.linalg.inv(affine)
    source_ijk = to_source[:3, :3] @ world + to_source[:3, 3:]
    values = map_coordinates(data, source_ijk, order=0, mode="constant", cval=0)
    return values.reshape(shape[:3])


def matching_regions(regions: list[Region], keywords: tuple[str, ...]) -> set[int]:
    """Values of the regions whose name contains any keyword (case-insensitive)."""
    words = [word.lower() for word in keywords]
    return {r.value for r in regions if any(word in r.name.lower() for word in words)}
