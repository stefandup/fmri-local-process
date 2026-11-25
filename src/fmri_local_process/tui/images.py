"""Pictures in the terminal: always written as a PNG file, shown inline where the terminal can
(sixel or kitty graphics), else just the file's path to copy."""

from collections.abc import Callable
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from fmri_local_process.app import figures

_inline: Callable[..., Widget] | None = None  # the widget that shows pictures here, if any


def detect() -> bool:
    """Ask the terminal whether it can show pictures. Only before the app starts: the answer
    arrives on stdin, which Textual owns afterwards."""
    global _inline
    from textual_image.renderable import Image as Best
    from textual_image.renderable import SixelImage, TGPImage
    from textual_image.widget import Image

    _inline = Image if Best in (SixelImage, TGPImage) else None
    return _inline is not None


class ImagePanel(Vertical):
    """One picture with its file's path underneath, or a message why there is none."""

    DEFAULT_CSS = """
    ImagePanel { height: auto; max-height: 45; }
    ImagePanel .picture { width: 100%; height: auto; max-height: 40; }
    ImagePanel .path { color: $text-muted; }
    """

    def __init__(self, message: str = "", id: str | None = None) -> None:
        super().__init__(id=id)
        self.message = message
        self.path: Path | None = None

    def compose(self) -> ComposeResult:
        yield Static(self.message, classes="path", markup=False)

    def show_message(self, text: str) -> None:
        self.path, self.message = None, text
        self._show(text)

    def show_png(self, data: bytes, name: str) -> None:
        self.show_file(figures.save_png(data, name))

    def show_file(self, path: Path) -> None:
        self.path = path
        self.message = f"Image file: {path}"
        self._show(self.message)

    def _show(self, text: str) -> None:
        self.query_one(".path", Static).update(text)
        for old in self.query(".picture"):
            old.remove()
        if _inline is not None and self.path is not None:
            self.mount(_inline(self.path, classes="picture"), before=0)
