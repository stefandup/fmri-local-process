"""Pick what goes into a review package (`.\\build.ps1 -Review` opens this, then builds).

The folders start as the toolbox's own (what fmri_toolbox / fmri_crosscheck open on), else
the last plan's, else blank. Tick subjects, sessions and scans in the tree (ticking a subject
ticks all its scans); the C-PAC output kinds, behavioural logs (those of the shared
tasks are ticked for you) and options follow, with the sizes they add. Raw DICOMs are left out
unless ticked. "Build package" saves the choices to the plan file and closes; build.ps1 then
builds the app and packs it with the data (processing/review_package.py). The plan file is
read back next time, so a rebuild starts from the last choices.

    python -m fmri_local_process.gui.review_package_dialog --plan <plan.json>
exit code 0: plan saved; 2: cancelled.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from fmri_local_process.gui.qt_common import (
    PROCESS_ACCENT_COLOR,
    PROCESS_ACCENT_HOVER_COLOR,
    apply_app_style,
    default_browse_dir,
    primary_action_stylesheet,
    style_secondary_label,
    wrap_tooltip,
)
from fmri_local_process.gui.toolbox_state import ToolboxState
from fmri_local_process.processing import review_package as rp
from fmri_local_process.processing.atlases import atlases_present, default_cache

FOLDERS = (
    ("bids", "BIDS folder"),
    ("output", "C-PAC output folder"),
    ("behav", "Behavioural logs folder"),
    ("dicom", "DICOM sessions folder"),
    ("mriqc", "MRIQC output folder"),
)
CANCELLED = 2
RECOUNT_DELAY_MS = 300
CHOICE_ROLE = Qt.ItemDataRole.UserRole


class ReviewPackageDialog(QDialog):
    def __init__(self, plan_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plan_path = plan_path
        self.setWindowTitle("Review package: what to share")
        self.resize(1250, 800)
        self.logs: list[rp.BehavLog] = []
        self.log_ticks: dict[str, bool] = {}  # ticks set by hand survive a refill
        self.name_edited = False
        self.recount = QTimer(self)
        self.recount.setSingleShot(True)
        self.recount.setInterval(RECOUNT_DELAY_MS)
        self.recount.timeout.connect(self.update_counts)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_folders())
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_scans())
        splitter.addWidget(self._build_right())
        splitter.setSizes([600, 650])
        layout.addWidget(splitter, 1)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.build_button = QPushButton("Build package")
        self.build_button.setStyleSheet(
            primary_action_stylesheet(PROCESS_ACCENT_COLOR, PROCESS_ACCENT_HOVER_COLOR)
        )
        self.build_button.clicked.connect(self.on_build)
        buttons.addWidget(self.build_button)
        layout.addLayout(buttons)

        previous = self.read_previous_plan()
        self.fill_folders(previous)
        self.fill_scans(previous)
        if previous is not None:
            self.name_edit.setText(previous.name)
            self.name_edited = True
            self.dicom_box.setChecked(previous.include_dicom)
            self.mriqc_box.setChecked(previous.include_mriqc)
            self.zip_box.setChecked(previous.make_zip)
            self.view_only_box.setChecked(previous.view_only)
            self.atlas_box.setChecked(previous.include_atlases and self.atlas_box.isEnabled())
            for i in range(self.output_list.count()):
                item = self.output_list.item(i)
                ticked = item.data(CHOICE_ROLE) in previous.outputs
                item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
            self.log_ticks = {key: True for key in previous.behav_logs}
        self.on_scans_changed()

    # ---- building ----------------------------------------------------------------------------

    def _build_folders(self) -> QGroupBox:
        group = QGroupBox("Data folders (the toolbox's own; change any here)")
        grid = QGridLayout(group)
        self.folder_edits: dict[str, QLineEdit] = {}
        for row, (key, label) in enumerate(FOLDERS):
            grid.addWidget(QLabel(label + ":"), row, 0)
            edit = QLineEdit()
            edit.editingFinished.connect(self.on_folders_changed)
            grid.addWidget(edit, row, 1)
            browse = QPushButton("Browse...")
            browse.clicked.connect(lambda _=False, k=key: self.browse(k))
            grid.addWidget(browse, row, 2)
            self.folder_edits[key] = edit
        grid.addWidget(QLabel("Package name:"), len(FOLDERS), 0)
        self.name_edit = QLineEdit()
        self.name_edit.textEdited.connect(lambda _text: setattr(self, "name_edited", True))
        grid.addWidget(self.name_edit, len(FOLDERS), 1)
        return group

    def _build_scans(self) -> QGroupBox:
        group = QGroupBox("Subjects, sessions and scans to share")
        layout = QVBoxLayout(group)
        note = QLabel("Only what is ticked goes in. Tick a subject to share all its scans.")
        style_secondary_label(note)
        layout.addWidget(note)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Scan", "Files", "Size"])
        self.tree.itemChanged.connect(lambda _item, _column: self.on_scans_changed())
        layout.addWidget(self.tree, 1)
        return group

    def _build_right(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        outputs = QGroupBox("C-PAC outputs (of the shared sessions and tasks)")
        outputs_layout = QVBoxLayout(outputs)
        self.output_list = QListWidget()
        for key, text in rp.OUTPUT_KINDS.items():
            item = QListWidgetItem(text)
            item.setData(CHOICE_ROLE, key)
            item.setCheckState(Qt.CheckState.Checked)
            self.output_list.addItem(item)
        self.output_list.itemChanged.connect(lambda _item: self.recount.start())
        outputs_layout.addWidget(self.output_list)
        layout.addWidget(outputs, 2)

        logs = QGroupBox("Behavioural logs (of the shared subjects)")
        logs_layout = QVBoxLayout(logs)
        self.log_list = QListWidget()
        self.log_list.itemChanged.connect(self.on_log_ticked)
        logs_layout.addWidget(self.log_list)
        layout.addWidget(logs, 2)

        options = QGroupBox("Options")
        options_layout = QVBoxLayout(options)
        self.dicom_box = QCheckBox("Include the raw DICOMs of the shared scans")
        self.dicom_box.setToolTip(
            wrap_tooltip(
                "Only the DICOM series the shared scans were converted from; never other "
                "scans or sessions. Lets the reviewer check the conversion and sequence "
                "parameters. Raw DICOMs can hold identifying header fields: share with care."
            )
        )
        self.dicom_box.toggled.connect(lambda _on: self.recount.start())
        options_layout.addWidget(self.dicom_box)
        self.mriqc_box = QCheckBox("Include the MRIQC reports and metrics of the shared scans")
        self.mriqc_box.setToolTip(
            wrap_tooltip(
                "Each shared run's MRIQC metrics, HTML report and its figures (from the MRIQC "
                "output folder). The group reports stay out: they hold every participant. "
                "The package then shows MRIQC's results but can't run MRIQC."
            )
        )
        self.mriqc_box.setChecked(True)
        self.mriqc_box.toggled.connect(lambda _on: self.recount.start())
        options_layout.addWidget(self.mriqc_box)
        self.atlas_box = QCheckBox("Include the first-level atlases (no Docker needed to view)")
        present = atlases_present(default_cache())
        self.atlas_box.setEnabled(present)
        self.atlas_box.setChecked(present)
        if not present:
            self.atlas_box.setToolTip(
                "No atlases fetched yet (First-level stats tab fetches them)."
            )
        options_layout.addWidget(self.atlas_box)
        self.view_only_box = QCheckBox("View only (hide Setup and Preprocess, lock changes)")
        self.view_only_box.setToolTip(
            wrap_tooltip(
                "The reviewer can look at everything but not convert, preprocess, trim, "
                "delete or save log corrections. Crosscheck marks and notes still work."
            )
        )
        self.view_only_box.setChecked(True)
        options_layout.addWidget(self.view_only_box)
        self.zip_box = QCheckBox("Also make a .zip of the package")
        self.zip_box.setChecked(True)
        options_layout.addWidget(self.zip_box)
        layout.addWidget(options)
        return panel

    # ---- folders -----------------------------------------------------------------------------

    def read_previous_plan(self) -> rp.Plan | None:
        try:
            return rp.Plan.from_json(self.plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def fill_folders(self, previous: rp.Plan | None) -> None:
        """The toolbox's current folders (as fmri_toolbox / fmri_crosscheck open them, older
        windows' remembered folders included), else the last plan's; blank if neither."""
        toolbox = ToolboxState()
        planned = {
            "bids": previous.bids_root if previous else None,
            "output": previous.output_root if previous else None,
            "behav": previous.behav_root if previous else None,
            "dicom": previous.dicom_root if previous else None,
            "mriqc": previous.mriqc_root if previous else None,
        }
        for key, edit in self.folder_edits.items():
            last = planned.get(key)
            folder = toolbox.folder(key) or (last if last and last.is_dir() else None)
            edit.setText(str(folder) if folder else "")

    def folder(self, key: str) -> Path | None:
        text = self.folder_edits[key].text().strip()
        return Path(text) if text and Path(text).is_dir() else None

    def browse(self, key: str) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, dict(FOLDERS)[key], default_browse_dir(self.folder(key))
        )
        if chosen:
            self.folder_edits[key].setText(chosen)
            self.on_folders_changed()

    def on_folders_changed(self) -> None:
        self.fill_scans(self.current_plan())
        self.on_scans_changed()

    # ---- scans -------------------------------------------------------------------------------

    def fill_scans(self, previous: rp.Plan | None) -> None:
        chosen = set(previous.scans) if previous else set()
        self.tree.blockSignals(True)
        self.tree.clear()
        bids = self.folder("bids")
        choices = rp.bids_scans(bids) if bids else []
        subjects: dict[str, QTreeWidgetItem] = {}
        sessions: dict[tuple[str, str], QTreeWidgetItem] = {}
        for choice in choices:
            if choice.subject not in subjects:
                item = QTreeWidgetItem([f"sub-{choice.subject}"])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsAutoTristate)
                self.tree.addTopLevelItem(item)
                subjects[choice.subject] = item
            key = (choice.subject, choice.session)
            if key not in sessions:
                item = QTreeWidgetItem(subjects[choice.subject], [f"ses-{choice.session}"])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsAutoTristate)
                sessions[key] = item
            files = scan_files(bids, choice) if bids else []
            leaf = QTreeWidgetItem(
                sessions[key], [choice.scan, str(len(files)), rp.human_size(rp.size_of(files))]
            )
            leaf.setData(0, CHOICE_ROLE, choice)
            leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            ticked = choice in chosen
            leaf.setCheckState(0, Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
        for item in subjects.values():
            item.setExpanded(any(c.subject == item.text(0)[4:] for c in chosen))
        self.tree.resizeColumnToContents(0)
        self.tree.blockSignals(False)

    def chosen_scans(self) -> list[rp.ScanChoice]:
        """The ticked scans (the tree's leaves carry their ScanChoice)."""
        found = []
        items = QTreeWidgetItemIterator(self.tree)
        while (item := items.value()) is not None:
            choice = item.data(0, CHOICE_ROLE)
            if choice is not None and item.checkState(0) == Qt.CheckState.Checked:
                found.append(choice)
            items += 1
        return found

    def on_scans_changed(self) -> None:
        plan = self.current_plan()
        if not self.name_edited:
            self.name_edit.setText(default_name(plan.subjects))
        self.fill_logs(plan)
        self.recount.start()

    # ---- logs --------------------------------------------------------------------------------

    def fill_logs(self, plan: rp.Plan) -> None:
        behav, bids = self.folder("behav"), self.folder("bids")
        self.logs = rp.behav_logs(behav, bids, plan.subjects) if behav and bids else []
        defaults = set(rp.default_behav_logs(self.logs, plan))
        self.log_list.blockSignals(True)
        self.log_list.clear()
        for log_file in self.logs:
            item = QListWidgetItem(
                f"sub-{log_file.subject}  {log_file.task or '?'}:  {log_file.key}"
            )
            item.setData(CHOICE_ROLE, log_file.key)
            ticked = self.log_ticks.get(log_file.key, log_file.key in defaults)
            item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
            self.log_list.addItem(item)
        self.log_list.blockSignals(False)

    def on_log_ticked(self, item: QListWidgetItem) -> None:
        self.log_ticks[item.data(CHOICE_ROLE)] = item.checkState() == Qt.CheckState.Checked
        self.recount.start()

    def chosen_logs(self) -> list[str]:
        return [
            item.data(CHOICE_ROLE)
            for item in (self.log_list.item(i) for i in range(self.log_list.count()))
            if item is not None and item.checkState() == Qt.CheckState.Checked
        ]

    # ---- plan and counts ---------------------------------------------------------------------

    def chosen_outputs(self) -> list[str]:
        return [
            item.data(CHOICE_ROLE)
            for item in (self.output_list.item(i) for i in range(self.output_list.count()))
            if item is not None and item.checkState() == Qt.CheckState.Checked
        ]

    def current_plan(self) -> rp.Plan:
        return rp.Plan(
            name=self.name_edit.text().strip() if hasattr(self, "name_edit") else "",
            bids_root=self.folder("bids") or Path(),
            output_root=self.folder("output"),
            behav_root=self.folder("behav"),
            dicom_root=self.folder("dicom"),
            mriqc_root=self.folder("mriqc"),
            scans=self.chosen_scans(),
            outputs=self.chosen_outputs(),
            behav_logs=self.chosen_logs(),
            include_dicom=self.dicom_box.isChecked(),
            include_mriqc=self.mriqc_box.isChecked(),
            include_atlases=self.atlas_box.isChecked(),
            view_only=self.view_only_box.isChecked(),
            make_zip=self.zip_box.isChecked(),
        )

    def update_counts(self) -> None:
        """Sizes per output kind and in total, for what is ticked now."""
        plan = self.current_plan()
        by_kind: dict[str, int] = {}
        for path, kind in rp.output_files(plan) if plan.scans else []:
            by_kind[kind] = by_kind.get(kind, 0) + rp.size_of([path])
        self.output_list.blockSignals(True)
        for i in range(self.output_list.count()):
            item = self.output_list.item(i)
            key = item.data(CHOICE_ROLE)
            item.setText(f"{rp.OUTPUT_KINDS[key]}   ({rp.human_size(by_kind.get(key, 0))})")
        self.output_list.blockSignals(False)
        bids = rp.size_of(rp.bids_files(plan)) if plan.scans else 0
        outputs = sum(by_kind.get(k, 0) for k in plan.outputs)
        logs = rp.size_of(plan.behav_root / k for k in plan.behav_logs) if plan.behav_root else 0
        parts = [f"BIDS {rp.human_size(bids)}", f"outputs {rp.human_size(outputs)}"]
        parts.append(f"{len(plan.behav_logs)} log(s) {rp.human_size(logs)}")
        total = bids + outputs + logs
        if plan.include_mriqc and plan.mriqc_root is not None:
            files = rp.mriqc_files(plan) if plan.scans else []
            mriqc = rp.size_of(files) if rp.has_mriqc_results(files) else 0
            parts.append(f"MRIQC {rp.human_size(mriqc)}")
            total += mriqc
        if plan.include_dicom:
            dicom = rp.size_of(rp.dicom_series_dirs(plan))
            parts.append(f"raw DICOM {rp.human_size(dicom)}")
            total += dicom
        self.summary.setText(
            f"{len(plan.subjects)} subject(s), {len(plan.scans)} scan(s):  "
            + ",  ".join(parts)
            + f"   =   {rp.human_size(total)} (plus the app, ~0.5 GB)"
        )
        self.build_button.setEnabled(bool(plan.scans and plan.name))

    def on_build(self) -> None:
        plan = self.current_plan()
        if not plan.scans or not plan.name:
            return
        if any(c in plan.name for c in '\\/:*?"<>|'):
            QMessageBox.warning(self, "Package name", 'Use a name without \\ / : * ? " < > |.')
            return
        if plan.include_dicom:
            answer = QMessageBox.question(
                self,
                "Raw DICOMs",
                "The raw DICOMs of the shared scans will go in. Their headers can hold "
                "identifying details (name, birth date). Share them anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.plan_path.parent.mkdir(parents=True, exist_ok=True)
        self.plan_path.write_text(plan.to_json(), encoding="utf-8")
        self.accept()


def scan_files(bids_root: Path, choice: rp.ScanChoice) -> list[Path]:
    folder = bids_root / f"sub-{choice.subject}" / f"ses-{choice.session}"
    return [p for p in folder.rglob("*") if p.is_file() and rp.scan_of(p.name) == choice.scan]


def default_name(subjects: list[str]) -> str:
    who = "_".join(f"sub-{s}" for s in subjects[:3]) or "nothing-ticked"
    if len(subjects) > 3:
        who += f"_and_{len(subjects) - 3}_more"
    return f"review_{who}_{date.today():%Y%m%d}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pick what goes into a review package.")
    parser.add_argument("--plan", type=Path, required=True, help="plan JSON to write")
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv)
    apply_app_style(app)  # type: ignore[arg-type]
    dialog = ReviewPackageDialog(args.plan)
    return 0 if dialog.exec() == QDialog.DialogCode.Accepted else CANCELLED


if __name__ == "__main__":
    sys.exit(main())
