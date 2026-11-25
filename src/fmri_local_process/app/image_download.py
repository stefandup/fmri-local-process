"""A Docker image a tab needs: downloading it, removing it, and remembering an unfinished
download across restarts."""

import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from fmri_local_process.app.settings import Settings
from fmri_local_process.processing.docker_pull import (
    PullProgress,
    pull_image,
    remove_image,
    remove_partial,
)

NO_DOCKER = "Docker isn't installed."


def pull_log(image: str) -> Path:
    return Path(tempfile.gettempdir()) / f"fmri_docker_pull_{re.sub(r'[^\w.-]', '_', image)}.txt"


def last_line(log_path: Path) -> str:
    """docker's last words in a pull log (its error, when it failed)."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    text = next((line.strip() for line in reversed(lines) if line.strip()), "")
    return text if len(text) <= 300 else text[:300] + "..."


class PullJob:
    def __init__(self, image: str, progress: Callable[[float, str], object]) -> None:
        self.image = image
        self.progress = progress  # 0..1 (-1: not measurable yet), what is done

    def run(self) -> str:
        """Why it failed, "" when it worked."""
        exe = shutil.which("docker")
        if exe is None:
            return NO_DOCKER
        log_path = pull_log(self.image)
        try:
            code = pull_image(Path(exe), self.image, log_path, self.relay)
        except OSError as e:
            return str(e)
        if code == 0:
            return ""
        return (
            f"{last_line(log_path) or f'docker pull exit {code}'}. Check the internet "
            "connection and click Download again: the parts already downloaded are kept."
        )

    def relay(self, progress: PullProgress) -> None:
        if not progress.stages:  # docker hasn't reported a layer yet: no measure to show
            self.progress(-1.0, "Starting the download...")
        else:
            self.progress(progress.fraction(), progress.text())


def remove(image: str, partial: bool) -> str:
    """Remove the image (or what an unfinished download left): why it failed, "" if it worked."""
    exe = shutil.which("docker")
    if exe is None:
        return NO_DOCKER
    return (remove_partial if partial else remove_image)(Path(exe), image) or ""


class ImageState:
    """What is known of one image. partial: how far an unfinished download got ('' if none);
    with settings, remembered across restarts."""

    def __init__(
        self, image: str, what: str, size_text: str, remove_note: str, settings: Settings | None
    ) -> None:
        self.image, self.what, self.size_text = image, what, size_text
        self.remove_note = remove_note  # added to the 'Remove?' question
        self.settings = settings
        self.partial = str(settings.value(self.partial_key, "") or "") if settings else ""
        self.present: bool | None = None

    @property
    def partial_key(self) -> str:
        return f"partial_downloads/{re.sub(r'[^\w.-]', '_', self.image)}"

    def set_partial(self, text: str) -> None:
        """Remember how far an unfinished download got ('' when none is unfinished)."""
        self.partial = text
        if self.settings is not None:
            if text:
                self.settings.setValue(self.partial_key, text)
            else:
                self.settings.remove(self.partial_key)

    def download_text(self) -> str:
        if self.partial:
            return f"Continue download ({self.partial})"
        return f"Download {self.what} ({self.size_text})"

    def remove_question(self) -> str:
        note = f"\n\n{self.remove_note}" if self.remove_note else ""
        return (
            f"Remove {self.image} from this computer, freeing its disk space "
            f"({self.size_text})?\n\nIt has to be downloaded again before it can be used. "
            f"Your data and results aren't touched.{note}"
        )

    def remove_partial_question(self) -> str:
        return (
            f"Give up on the unfinished download of {self.image} ({self.partial}) and clear "
            "what it left on disk?\n\nThis also removes other unused, untagged Docker images "
            "(old versions nothing refers to). Your data and results aren't touched. "
            "Downloading later starts from the beginning."
        )
