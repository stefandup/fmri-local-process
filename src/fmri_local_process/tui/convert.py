"""Setup & Convert to BIDS: the folders, the study's settings, the converter, its report, the scan
rules and the crosscheck backup."""

import logging
from datetime import datetime
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, ProgressBar, Static

from fmri_local_process.app import backup
from fmri_local_process.app.convert import (
    DEFACER_WARNINGS,
    HELP_TEXT,
    MAX_SESSIONS,
    PACKAGE_LOGGER,
    ConvertJob,
    LineHandler,
    check_defacer,
    replay,
    report_lines,
    report_rows,
    restore_question,
    setup_notices,
)
from fmri_local_process.app.scan_rules import HELP_TEXT as RULES_HELP
from fmri_local_process.app.scan_rules import check_text
from fmri_local_process.app.settings import is_on, saved_number
from fmri_local_process.app.state import CONVERTING
from fmri_local_process.processing.crosscheck import (
    read_expected_sessions,
    write_expected_sessions,
)
from fmri_local_process.processing.crosscheck_backup import (
    list_backups,
    restore_backup,
    save_backup,
)
from fmri_local_process.processing.philani_bids import (
    SCAN_RULES,
    ConversionReport,
    ScanRule,
    read_scan_rules,
    rule_from,
    write_scan_rules,
)
from fmri_local_process.tui.widgets import (
    Activity,
    FolderBox,
    Line,
    Notices,
    Page,
    PickScreen,
    Table,
    TextScreen,
    cell,
    plain,
    whole_number,
)


class RulesScreen(ModalScreen[bool]):
    """Scan rules: which DICOM series folder becomes which BIDS scan."""

    BINDINGS = [("escape", "dismiss(False)", "Cancel")]

    def __init__(self, bids_root: Path, dicom_root: Path | None) -> None:
        super().__init__()
        self.bids_root, self.dicom_root = bids_root, dicom_root
        self.rules: list[ScanRule] = list(read_scan_rules(bids_root))

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Static("Scan rules", classes="dialog-title")
            yield Table(["Folder pattern", "BIDS scan"], id="rules")
            with Horizontal(classes="row"):
                yield Input(placeholder="Folder pattern, e.g. *t1_mprage*", id="pattern")
                yield Input(placeholder="BIDS scan, e.g. T1w", id="scan")
                yield Button("Add rule", id="add")
            yield Line(id="rule-problem")
            with Horizontal(classes="buttons"):
                yield Button("Remove highlighted", id="remove")
                yield Button("Reset to Philani defaults", id="reset")
                yield Button("Check DICOM folder", id="check", disabled=self.dicom_root is None)
                yield Button("Help", id="help")
            yield Line(id="report")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.fill()

    def fill(self) -> None:
        rows = [(str(i), (rule.pattern, rule.scan)) for i, rule in enumerate(self.rules)]
        self.query_one("#rules", Table).fill(rows)

    def problem(self, text: str) -> None:
        self.query_one("#rule-problem", Line).set(text, "#e74c3c")

    @on(Button.Pressed, "#add")
    def add(self) -> None:
        pattern, scan = self.query_one("#pattern", Input), self.query_one("#scan", Input)
        try:
            self.rules.append(rule_from(pattern.value, scan.value))
        except ValueError as e:
            self.problem(str(e))
            return
        pattern.value = scan.value = ""
        self.problem("")
        self.fill()

    @on(Button.Pressed, "#remove")
    def remove_rule(self) -> None:
        key = self.query_one("#rules", Table).current_key()
        if key is not None:
            del self.rules[int(key)]
            self.fill()

    @on(Button.Pressed, "#reset")
    def reset(self) -> None:
        self.rules = list(SCAN_RULES)
        self.fill()

    @on(Button.Pressed, "#check")
    def check(self) -> None:
        if self.dicom_root is None:
            return
        try:
            text = check_text(self.dicom_root, tuple(self.rules))
        except OSError as e:
            text = f"Can't read the DICOM folder: {e}"
        self.query_one("#report", Line).set(text)

    @on(Button.Pressed, "#help")
    def show_help(self) -> None:
        self.app.push_screen(TextScreen("Scan rules", plain(RULES_HELP)))

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        if not self.rules:
            self.problem("Keep at least one rule.")
            return
        try:
            write_scan_rules(self.bids_root, tuple(self.rules))
        except OSError as e:
            self.problem(str(e))
            return
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(False)


class ConvertPage(Page):
    GUIDE = HELP_TEXT

    def compose(self) -> ComposeResult:
        settings = self.state.settings
        yield Notices()
        yield FolderBox(
            self.state,
            "dicom",
            "DICOM sessions (raw)",
            "One folder per scan session, named <YYYYMMDD>_<time>_<subject ID>. Only read.",
        )
        yield FolderBox(
            self.state,
            "behav",
            "Behavioural logs (raw, optional)",
            "The top folder of the behavioural logs. Only read.",
        )
        yield FolderBox(self.state, "bids", "BIDS folder", "Where Convert to BIDS writes.")
        with Horizontal(classes="row"):
            yield Static("Sessions per subject:", classes="label")
            yield Input(id="sessions", max_length=2)
            yield Checkbox(
                "Deface T1w",
                is_on(settings.value("deface", "true")),
                id="deface",
                disabled=True,
            )
            yield Button("Check again", id="check-defacer")
        yield Line(id="defacer")
        with Horizontal(classes="row"):
            yield Button("Convert to BIDS", id="convert", variant="success")
            yield Button("Scan rules...", id="rules")
        yield Line(id="lock")
        with Vertical(id="converting", classes="row"):
            yield ProgressBar(id="convert-bar", show_eta=False)
            yield Line(id="convert-now", muted=True)
        yield Static("Crosscheck backup", classes="section")
        with Horizontal(classes="row"):
            yield Static("Study ID:", classes="label")
            yield Input(str(settings.value("study_id", backup.DEFAULT_STUDY_ID)), id="study")
            yield Button("Save now", id="backup-save")
            yield Button("Restore...", id="backup-restore")
        with Horizontal(classes="row"):
            yield Checkbox(
                "Auto-save every", is_on(settings.value("autosave", "true")), id="autosave"
            )
            minutes = saved_number(settings, "autosave_minutes", backup.DEFAULT_AUTOSAVE_MINUTES)
            yield Input(str(int(minutes)), id="minutes", max_length=2)
            yield Static("minutes", classes="label")
        yield Line(id="backup-status", muted=True)
        yield Static("Last conversion", classes="section")
        yield Table(["Result", "Count"], id="summary")
        yield Activity()

    def on_mount(self) -> None:
        self.job: ConvertJob | None = None
        self.defacer_error = ""  # why defacing isn't available ("" when it is)
        self.checking_defacer = False
        self.replay_after_conversion = False  # set by a restore
        self.handler = LineHandler(lambda line: self.tell(self.activity.say, line))
        self.query_one("#converting").display = False
        self.state.folder_changed.connect(lambda _key: self.show_page())
        self.state.busy_changed.connect(self.update_controls)
        self.backup_timer = self.set_interval(60, self.autosave, pause=True)
        self.minutes_left = 0
        self.start_autosave()
        self.show_page()
        if self.toolbox.view_only:
            return
        self.check_defacer()

    @property
    def activity(self) -> Activity:
        return self.query_one(Activity)

    def show_page(self) -> None:
        bids = self.state.folder("bids")
        sessions = self.query_one("#sessions", Input)
        if not sessions.has_focus:
            sessions.value = str(read_expected_sessions(bids) if bids else 1)
        sessions.disabled = bids is None
        self.query_one(Notices).set_messages(setup_notices(self.state))
        self.update_controls()

    def update_controls(self) -> None:
        dicom, bids = self.state.folder("dicom"), self.state.folder("bids")
        reason = self.state.lock_reason(CONVERTING)
        self.query_one("#lock", Line).set(f"Locked: {reason}." if reason else "", "#f39c12")
        ready = dicom is not None and bids is not None and self.job is None and not reason
        self.query_one("#convert", Button).disabled = not ready
        self.query_one("#deface", Checkbox).disabled = (
            self.job is not None or self.checking_defacer or bool(self.defacer_error)
        )

    # ---- study settings -----------------------------------------------------------------------

    @on(Input.Submitted, "#sessions")
    def on_sessions(self, event: Input.Submitted) -> None:
        bids = self.state.folder("bids")
        sessions = whole_number(event.value, 1, MAX_SESSIONS)
        if bids is None:
            return
        if sessions is None:
            self.show_text("Not saved", f"Give a whole number from 1 to {MAX_SESSIONS}.")
            return
        try:
            write_expected_sessions(bids, sessions)
        except OSError as e:
            self.show_text("Not saved", str(e))
            return
        self.activity.say(f"Sessions per subject: {sessions} (saved)")
        self.state.bump()

    @on(Checkbox.Changed, "#deface")
    def on_deface(self, event: Checkbox.Changed) -> None:
        self.state.settings.setValue("deface", event.value)

    @on(Button.Pressed, "#rules")
    def on_rules(self) -> None:
        bids = self.state.folder("bids")
        if bids is None:
            self.show_text(
                "BIDS folder needed", "Choose the BIDS folder first: the rules are kept in it."
            )
            return

        def closed(saved: bool | None) -> None:
            if saved:
                self.activity.say("Scan rules saved: Convert to BIDS to apply them.")
                self.state.bump()

        self.app.push_screen(RulesScreen(bids, self.state.folder("dicom")), closed)

    # ---- defacer ------------------------------------------------------------------------------

    @on(Button.Pressed, "#check-defacer")
    def check_defacer(self) -> None:
        if self.checking_defacer:
            return
        self.checking_defacer = True
        self.query_one("#defacer", Line).set("Checking for a defacer...")
        self.update_controls()
        self.start_job(
            check_defacer,
            lambda result: self.on_defacer(*result),
            lambda error: self.on_defacer(error, ""),
        )

    def on_defacer(self, error: str, docker: str) -> None:
        self.checking_defacer = False
        self.defacer_error = error
        text = f"{DEFACER_WARNINGS[docker]}: {error}" if error else ""
        self.query_one("#defacer", Line).set(text, "#f39c12")
        self.query_one("#check-defacer").display = bool(error)
        self.update_controls()

    # ---- converting ---------------------------------------------------------------------------

    @on(Button.Pressed, "#convert")
    def convert(self) -> None:
        bids, dicom = self.state.folder("bids"), self.state.folder("dicom")
        if bids is None or dicom is None or self.job is not None or self.state.lock_reason():
            return
        deface = self.query_one("#deface", Checkbox).value and not self.defacer_error
        self.job = ConvertJob(
            dicom,
            bids,
            self.state.folder("behav"),
            deface,
            lambda done, total, what: self.tell(self.on_progress, done, total, what),
        )
        self.activity.say(f"Converting {dicom} -> {bids} ...")
        logging.getLogger(PACKAGE_LOGGER).addHandler(self.handler)
        self.query_one("#converting").display = True
        self.query_one("#convert-bar", ProgressBar).update(total=None)
        self.query_one("#convert-now", Line).set("Finding sessions and behavioural logs...")
        self.state.set_busy(CONVERTING, True)
        self.update_controls()
        self.start_job(self.job.run, self.on_converted, self.on_converted)

    def on_progress(self, done: int, total: int, what: str) -> None:
        self.query_one("#convert-bar", ProgressBar).update(total=max(total, 1), progress=done)
        self.query_one("#convert-now", Line).set(
            f"Now: {what}" if done < total else "Finishing up..."
        )

    def on_converted(self, result: ConversionReport | str) -> None:
        logging.getLogger(PACKAGE_LOGGER).removeHandler(self.handler)
        self.job = None
        self.query_one("#converting").display = False
        self.state.set_busy(CONVERTING, False)
        wanted, self.replay_after_conversion = self.replay_after_conversion, False
        if isinstance(result, str):
            self.activity.say(f"ERROR  {result}", error=True)
            self.show_text("Conversion failed", result)
        else:
            self.show_report(result)
            if wanted:
                self.replay_decisions()
        self.state.bump()

    def show_report(self, report: ConversionReport) -> None:
        self.query_one("#summary", Table).fill(
            [
                (what, (cell(what, color if n else ""), cell(n, color if n else "")))
                for what, n, color in report_rows(report)
            ]
        )
        self.activity.say("Done.")
        for line in report_lines(report):
            self.activity.say(line)

    # ---- backup -------------------------------------------------------------------------------

    def study_id(self) -> str:
        return backup.safe_name(self.query_one("#study", Input).value)

    def backup_folder(self) -> Path:
        return backup.backup_base() / self.study_id()

    @on(Input.Changed, "#study")
    def on_study(self, event: Input.Changed) -> None:
        self.state.settings.setValue("study_id", event.value.strip())

    @on(Checkbox.Changed, "#autosave")
    @on(Input.Submitted, "#minutes")
    def start_autosave(self) -> None:
        on = self.query_one("#autosave", Checkbox).value
        typed = self.query_one("#minutes", Input).value
        minutes = whole_number(typed, 1, backup.MAX_AUTOSAVE_MINUTES)
        self.state.settings.setValue("autosave", on)
        if minutes is None:
            minutes = backup.DEFAULT_AUTOSAVE_MINUTES
            self.query_one("#minutes", Input).value = str(minutes)
        self.state.settings.setValue("autosave_minutes", minutes)
        self.minutes_left = minutes
        if on:
            self.backup_timer.resume()
        else:
            self.backup_timer.pause()

    def autosave(self) -> None:
        self.minutes_left -= 1
        if self.minutes_left <= 0:
            saved = saved_number(self.state.settings, "autosave_minutes", 1)
            self.minutes_left = int(saved)
            self.save_backup(manual=False)

    @on(Button.Pressed, "#backup-save")
    def on_save_backup(self) -> None:
        self.save_backup(manual=True)

    def save_backup(self, manual: bool) -> list[Path]:
        bids = self.state.folder("bids")
        if bids is None:
            return []
        try:
            saved = save_backup(bids, self.backup_folder())
        except OSError as e:
            self.activity.say(f"ERROR  backup not saved: {e}", error=True)
            return []
        if saved:
            self.query_one("#backup-status", Line).set(
                f"Saved {len(saved)} file(s) at {datetime.now():%H:%M} in {self.backup_folder()}"
            )
            self.activity.say(backup.saved_message(len(saved), self.backup_folder()))
        elif manual:
            self.activity.say(backup.NOTHING_TO_SAVE)
        return saved

    @on(Button.Pressed, "#backup-restore")
    def on_restore(self) -> None:
        bids = self.state.folder("bids")
        if bids is None or self.state.folder("dicom") is None:
            self.show_text("Folders needed", "Choose the DICOM sessions and BIDS folders first.")
            return
        if reason := self.state.lock_reason():
            self.show_text("Not now", f"Can't restore now: {reason}.")
            return
        backups = list_backups(self.backup_folder())
        if not backups:
            self.show_text(
                "No backup yet", backup.no_backup_text(self.study_id(), self.backup_folder())
            )
            return

        def picked(index: int | None) -> None:
            if index is not None:
                chosen = backups[index]
                question = restore_question(chosen, bids)
                self.ask("Restore crosscheck", question, lambda: self.restore(chosen, bids))

        title = "Restore which saved copy (latest, or a day's copy)?"
        self.app.push_screen(PickScreen(title, [b.name for b in backups]), picked)

    def restore(self, saved: Path, bids: Path) -> None:
        try:
            restored = restore_backup(saved, bids)
        except OSError as e:
            self.show_text("Not restored", str(e))
            return
        self.activity.say(
            f"Restored {len(restored)} crosscheck file(s) from {saved}; converting..."
        )
        self.state.bump()
        self.replay_after_conversion = True
        self.convert()

    def replay_decisions(self) -> None:
        bids = self.state.folder("bids")
        if bids is None:
            return
        lines, text, _complete = replay(bids)
        for line in lines:
            self.activity.say(line)
        self.show_text("Restore finished", text)
