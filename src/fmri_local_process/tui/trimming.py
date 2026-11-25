"""Trimming long BOLD runs, shared by the Crosscheck and Scan lengths tabs (after confirming)."""

from collections.abc import Callable

from fmri_local_process.app.state import TRIMMING
from fmri_local_process.app.trimming import TrimJob, confirm_text
from fmri_local_process.processing.crosscheck import DataFile
from fmri_local_process.tui.widgets import Page


def start_trimming(
    page: Page, jobs: list[tuple[DataFile, int]], say: Callable[[str], object]
) -> None:
    state, bids_root = page.state, page.state.folder("bids")
    if not jobs or bids_root is None:
        return
    if reason := state.lock_reason():
        page.show_text("Not now", f"Can't trim now: {reason}.")
        return

    def start() -> None:
        if state.lock_reason():  # something started while the question was open
            return
        job = TrimJob(bids_root, jobs, lambda line: page.tell(say, line))
        state.set_busy(TRIMMING, True)
        page.start_job(job.run, lambda _result: finished(), failed)

    def finished() -> None:
        state.set_busy(TRIMMING, False)
        state.bump()

    def failed(error: str) -> None:
        say(f"ERROR  {error}")
        finished()

    page.ask("Confirm", confirm_text(jobs), start)
