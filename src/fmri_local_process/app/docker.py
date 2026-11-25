"""Docker's state on the tabs that run Docker (Preprocess: C-PAC, Scan quality: MRIQC)."""

from fmri_local_process.app.colors import ERROR_COLOR, OK_COLOR, RUNNING_COLOR, WARNING_COLOR

DOCKER_RECHECK_MS = 30_000


def docker_line(
    running: bool | None, error: str, warning: str, action: str = ""
) -> tuple[str, str]:
    """(text, colour, '' for plain). running None: not known yet; action: a start / restart
    in progress."""
    if action:
        return f"Docker: '{action}' in progress (up to a few minutes)...", RUNNING_COLOR
    if running is None:
        return "Checking Docker...", ""
    if not running:
        return f"⚠ Docker isn't running: {error}", ERROR_COLOR
    if warning:
        return f"⚠ {warning}", WARNING_COLOR
    return "✓ Docker is running.", OK_COLOR
