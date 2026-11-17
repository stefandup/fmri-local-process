"""fmri_toolbox: the Philani fMRI steps in one window, one tab per step, in the order of the
work: Setup & Convert to BIDS, ID & Behaviour Logs, Scan lengths & trimming, BIDS Crosscheck
(everything in BIDS correct before preprocessing), Preprocess BIDS (C-PAC), Crosscheck overview,
First-level stats, Sequence overview.

The tabs share their folders (chosen on the first tab) and know what the others are doing: while one
job changes the BIDS folder (converting, trimming, C-PAC reading it), the others' changing
actions are locked. A tab whose data changed refreshes when it is shown.

`fmri_crosscheck` and `fmri_preprocess_gui` open this window on their tab.
"""

import logging
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.gui.behav_logs_dialog import BehavLogsPage
from fmri_local_process.gui.convert_page import ConvertPage
from fmri_local_process.gui.crosscheck_gui import CrosscheckPage
from fmri_local_process.gui.first_level_page import FirstLevelPage
from fmri_local_process.gui.overview_page import OverviewPage
from fmri_local_process.gui.preprocess_gui import PreprocessWindow
from fmri_local_process.gui.qt_common import (
    NAME_LABEL_FONT_POINT_INCREASE,
    WARNING_COLOR,
    apply_app_style,
    shrinkable,
    style_secondary_label,
)
from fmri_local_process.gui.scan_lengths import ScanLengthsPage
from fmri_local_process.gui.sequence_page import SequencePage
from fmri_local_process.gui.toolbox_state import (
    CONVERTING,
    PREPROCESSING,
    TRIMMING,
    ToolboxState,
)
from fmri_local_process.gui.trimming import Trimmer

SETUP = "Setup & Convert to BIDS"
BEHAV = "ID & Behaviour Logs"
SCAN_LENGTHS = "Scan lengths & trimming"
CROSSCHECK = "BIDS Crosscheck"
PREPROCESS = "Preprocess BIDS (C-PAC)"
OVERVIEW = "Crosscheck overview"
FIRST_LEVEL = "First-level stats"
SEQUENCE = "Sequence overview"
TAB_ORDER = (
    SETUP, BEHAV, SCAN_LENGTHS, CROSSCHECK, PREPROCESS, OVERVIEW, FIRST_LEVEL, SEQUENCE
)  # fmt: skip
HEADER_POINT_INCREASE = NAME_LABEL_FONT_POINT_INCREASE // 2
LOGO_IMAGE = Path(__file__).parent / "assets" / "table_mountain.png"  # top right corner
LOGO_HEIGHT = 40


def scrolling(page: QWidget) -> QScrollArea:
    """A page that needs more room than the window has scrolls, instead of widening every
    tab (the Preprocess panels are wide)."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(page)
    area.setFrameShape(QFrame.Shape.NoFrame)
    return area


def tab_text(number: int, name: str, badge: str = "") -> str:
    """'4. Crosscheck (6 to check)'; '&' doubled, or Qt shows it as a shortcut underline."""
    return f"{number}. {name}".replace("&", "&&") + (f" ({badge})" if badge else "")


class ToolboxWindow(QMainWindow):
    def __init__(self, start_tab: str = SETUP) -> None:
        super().__init__()
        self.setWindowTitle("Philani fMRI toolbox")
        self.resize(1400, 900)
        self.state = ToolboxState()
        self.trimmer = Trimmer(self.state)

        self.convert = ConvertPage(self.state)
        self.behav = BehavLogsPage(self.state)
        self.crosscheck = CrosscheckPage(self.state, self.trimmer)
        self.scan_lengths = ScanLengthsPage(self.state, self.trimmer)
        self.preprocess = PreprocessWindow()
        self.preprocess.setWindowFlags(Qt.WindowType.Widget)  # a page here, not a window
        self.preprocess_generation = self.state.generation  # BIDS contents it last read
        self.overview = OverviewPage(self.state)
        self.first_level = FirstLevelPage(self.state)
        self.sequence = SequencePage(self.state)
        self.pages: dict[str, QWidget] = {
            SETUP: self.convert,
            BEHAV: self.behav,
            SCAN_LENGTHS: self.scan_lengths,
            CROSSCHECK: self.crosscheck,
            PREPROCESS: scrolling(self.preprocess),
            OVERVIEW: self.overview,
            FIRST_LEVEL: self.first_level,
            SEQUENCE: self.sequence,
        }

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(self._build_header())
        self.tabs = QTabWidget()
        for number, name in enumerate(TAB_ORDER, start=1):
            self.tabs.addTab(self.pages[name], tab_text(number, name))
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self._connect_preprocess()
        self.behav.badge_changed.connect(lambda text: self.set_badge(BEHAV, text))
        self.state.folder_changed.connect(self.on_folder_changed)
        self.state.busy_changed.connect(self.on_busy_changed)
        self.state.data_changed.connect(self.on_data_changed)
        self.tabs.currentChanged.connect(self.on_tab_changed)
        self.update_header()
        self.tabs.setCurrentIndex(TAB_ORDER.index(start_tab))
        self.on_tab_changed(self.tabs.currentIndex())
        self.refresh_crosscheck_badge()
        # Read the behavioural logs in the background from the start: tab 2 is ready by
        # the time you get to it.
        QTimer.singleShot(0, self.behav.show_page)

    def _build_header(self) -> QHBoxLayout:
        """The BIDS folder everything works on, and what is running."""
        row = QHBoxLayout()
        self.bids_name = QLabel()
        font = self.bids_name.font()
        font.setPointSize(font.pointSize() + HEADER_POINT_INCREASE)
        font.setBold(True)
        self.bids_name.setFont(font)
        row.addWidget(self.bids_name)
        self.bids_path = shrinkable(QLabel())
        style_secondary_label(self.bids_path)
        row.addWidget(self.bids_path, 1)
        self.busy_label = QLabel()
        self.busy_label.setStyleSheet(f"color: {WARNING_COLOR}; font-weight: bold;")
        row.addWidget(self.busy_label)
        self.logo = QLabel()
        logo = QPixmap(str(LOGO_IMAGE))
        if not logo.isNull():  # a missing asset just leaves the corner empty
            self.logo.setPixmap(
                logo.scaledToHeight(LOGO_HEIGHT, Qt.TransformationMode.SmoothTransformation)
            )
            self.logo.setToolTip("Table Mountain")
        row.addWidget(self.logo)
        return row

    def _connect_preprocess(self) -> None:
        """Preprocess keeps its own folder panels: keep them and the shared folders in step."""
        self.preprocess.use_folders(self.state.folder("bids"), self.state.folder("output"))
        for key in ("bids", "output"):  # it may remember folders the toolbox doesn't have yet
            if self.state.folder(key) is None:
                mine = (
                    self.preprocess.bids_folder if key == "bids" else self.preprocess.output_folder
                )
                self.state.set_folder(key, mine)
        self.preprocess.folder_chosen.connect(self.state.set_folder)
        self.preprocess.running_changed.connect(
            lambda running: self.state.set_busy(PREPROCESSING, running)
        )

    # ---- reacting to the shared state -------------------------------------------------------

    def update_header(self) -> None:
        bids_root = self.state.folder("bids")
        self.bids_name.setText(bids_root.name if bids_root else "No BIDS folder chosen")
        self.bids_path.setText(str(bids_root.parent) if bids_root else "(first tab)")
        self.bids_path.setToolTip(str(bids_root) if bids_root else "")
        busy = self.state.lock_reason()
        self.busy_label.setText(f"⏳ {busy}" if busy else "")

    def on_folder_changed(self, key: str) -> None:
        if key in ("bids", "output"):
            self.preprocess.use_folders(self.state.folder("bids"), self.state.folder("output"))
        self.update_header()

    def on_busy_changed(self) -> None:
        self.update_header()
        self.preprocess.set_external_lock(self.state.lock_reason(PREPROCESSING))

    def on_data_changed(self) -> None:
        current = self.tabs.currentWidget()
        if current is not None:
            self.show_page(current)
        self.refresh_crosscheck_badge()

    def on_tab_changed(self, _index: int) -> None:
        current = self.tabs.currentWidget()
        if current is not None:
            self.show_page(current)

    def show_page(self, page: QWidget) -> None:
        if isinstance(page, QScrollArea) and (inner := page.widget()) is not None:
            page = inner
        if page is self.preprocess and self.preprocess_generation != self.state.generation:
            # It keeps its own subject and task lists; re-read them after other tabs'
            # changes, or new conversions only show after a relaunch.
            self.preprocess_generation = self.state.generation
            self.preprocess.reload_bids()
        show = getattr(page, "show_page", None)
        if callable(show):
            show()
        if page is self.crosscheck:
            self.set_badge(CROSSCHECK, self.crosscheck.badge())

    def refresh_crosscheck_badge(self) -> None:
        if self.crosscheck.seen_generation == self.state.generation:
            self.set_badge(CROSSCHECK, self.crosscheck.badge())

    def set_badge(self, name: str, text: str) -> None:
        number = TAB_ORDER.index(name) + 1
        self.tabs.setTabText(number - 1, tab_text(number, name, text))

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 -- Qt override
        working = CONVERTING in self.state.busy or TRIMMING in self.state.busy
        if working:
            answer = QMessageBox.question(
                self,
                "Still working",
                f"{self.state.lock_reason(PREPROCESSING)}. Closing now stops it: the scan "
                "it is on is left out of the BIDS folder and done again next time. Close anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.preprocess.closeEvent(event)  # asks about a running C-PAC, stops its threads
        threads = self.behav.loader is not None or self.convert.defacer_probe is not None
        if event.isAccepted() and (working or threads):
            # A QThread destroyed while running aborts with a crash dialog: leave at once.
            os._exit(0)


def start(app: QApplication, start_tab: str = SETUP) -> ToolboxWindow:
    """Style the app and show the toolbox window (the launcher's splash covers this)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    apply_app_style(app)
    window = ToolboxWindow(start_tab)
    window.show()
    return window


def main(start_tab: str = SETUP) -> None:
    """The toolbox with its splash screen (see launcher.py)."""
    from fmri_local_process.gui.launcher import launch

    launch(start_tab)


if __name__ == "__main__":
    main()
