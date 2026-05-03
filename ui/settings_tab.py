"""Two-panel S-register editor for local + remote RFD900 radios.

Renders every S-register (S0..S15) and pin-mapping register (R0..R15) for both
the local and the remote radio, side-by-side, as live editor widgets bound to
a :class:`rfd.radio.Radio`. Loads parameters via ATI5/RTI5, tracks per-row
dirty state, and writes only the modified registers on demand. Built-in
profiles and JSON profile import/export are exposed via a Profiles menu.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from rfd.presets import (
    BUILT_IN_PRESETS,
    Profile,
    load_profile,
    save_profile,
)
from rfd.radio import Radio
from rfd.registers import (
    PIN_REGISTERS,
    REGISTERS,
    RegisterDef,
    all_pin_registers,
    all_registers,
    validate,
)


# Connected states in which the action buttons are usable. Anything else
# (disconnected/bootloader) means the radio cannot service AT commands.
_LIVE_STATES: frozenset[str] = frozenset({Radio.STATE_DATA, Radio.STATE_COMMAND})

# QSS applied to a row's editor while it has unsaved changes — a soft amber
# tint that is visible on both the light and dark Qt fusion palettes.
_DIRTY_QSS = "background-color: #fff4c2;"


@dataclass
class _Row:
    """One register row inside a panel."""

    sreg: int
    is_pin: bool
    reg: RegisterDef
    label: QLabel
    editor: QWidget
    dirty_marker: QLabel

    def value(self) -> int:
        if isinstance(self.editor, QSpinBox):
            return int(self.editor.value())
        if isinstance(self.editor, QComboBox):
            data = self.editor.currentData()
            return int(data) if data is not None else 0
        return 0

    def set_value(self, value: int) -> None:
        # Block signals so programmatic updates from the radio don't mark the
        # row dirty.
        self.editor.blockSignals(True)
        try:
            if isinstance(self.editor, QSpinBox):
                self.editor.setValue(int(value))
            elif isinstance(self.editor, QComboBox):
                idx = self.editor.findData(int(value))
                if idx < 0:
                    # Unknown enum option — append it so the value round-trips.
                    self.editor.addItem(f"{value} (unknown)", int(value))
                    idx = self.editor.findData(int(value))
                self.editor.setCurrentIndex(idx)
        finally:
            self.editor.blockSignals(False)


def _make_editor(reg: RegisterDef, parent: QWidget) -> QWidget:
    """Build the editor widget appropriate for ``reg.kind``."""
    if reg.kind == "int":
        sb = QSpinBox(parent)
        lo = reg.minimum if reg.minimum is not None else 0
        hi = reg.maximum if reg.maximum is not None else 0xFFFF
        sb.setRange(int(lo), int(hi))
        if reg.default is not None:
            sb.setValue(int(reg.default))
        if reg.units:
            sb.setSuffix(f" {reg.units}")
        sb.setToolTip(reg.tooltip)
        if reg.read_only:
            sb.setEnabled(False)
        return sb

    if reg.kind == "enum":
        cb = QComboBox(parent)
        if reg.enum:
            for k in sorted(reg.enum.keys()):
                cb.addItem(f"{reg.enum[k]} ({k})", int(k))
        if reg.default is not None:
            idx = cb.findData(int(reg.default))
            if idx >= 0:
                cb.setCurrentIndex(idx)
        cb.setToolTip(reg.tooltip)
        if reg.read_only:
            cb.setEnabled(False)
        return cb

    if reg.kind == "bool":
        cb = QComboBox(parent)
        cb.addItem("Off (0)", 0)
        cb.addItem("On (1)", 1)
        if reg.default is not None:
            cb.setCurrentIndex(1 if int(reg.default) else 0)
        cb.setToolTip(reg.tooltip)
        if reg.read_only:
            cb.setEnabled(False)
        return cb

    # Fallback — never expected, but keep the UI alive if a new kind appears.
    fallback = QLabel(f"<unsupported kind {reg.kind!r}>", parent)
    fallback.setEnabled(False)
    return fallback


class _RegisterPanel(QWidget):
    """One side of the editor: every S-register plus an R-register group."""

    # Emitted whenever the user edits any field in this panel.
    value_changed = Signal(int, bool)  # sreg, is_pin
    # Emitted on focus-in so the parent tab can track which panel is "active".
    interacted = Signal()

    def __init__(self, title: str, is_remote: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_remote = is_remote
        self._rows: dict[tuple[int, bool], _Row] = {}
        self._dirty: set[tuple[int, bool]] = set()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        header = QLabel(f"<b>{title}</b>", self)
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(header)

        # Scroll area wrapping the actual register grid + pin group.
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget(scroll)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(4, 4, 4, 4)
        body_layout.setSpacing(2)

        # ---- S-register rows -----------------------------------------
        for reg in all_registers():
            row_widget, row = self._build_row(reg, is_pin=False, parent=body)
            body_layout.addWidget(row_widget)
            self._rows[(reg.sreg, False)] = row

        # ---- Pin map group (collapsible, RFD900x/ux only) ------------
        self._pin_group = QGroupBox("Pin mapping (RFD900x/ux only)", body)
        self._pin_group.setCheckable(True)
        self._pin_group.setChecked(False)
        pin_inner = QWidget(self._pin_group)
        pin_layout = QVBoxLayout(pin_inner)
        pin_layout.setContentsMargins(4, 4, 4, 4)
        pin_layout.setSpacing(2)
        for reg in all_pin_registers():
            row_widget, row = self._build_row(reg, is_pin=True, parent=pin_inner)
            pin_layout.addWidget(row_widget)
            self._rows[(reg.sreg, True)] = row
        group_layout = QVBoxLayout(self._pin_group)
        group_layout.setContentsMargins(6, 6, 6, 6)
        group_layout.addWidget(pin_inner)
        self._pin_inner = pin_inner
        pin_inner.setVisible(False)
        self._pin_group.toggled.connect(pin_inner.setVisible)
        body_layout.addWidget(self._pin_group)

        body_layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    # ---------------------------------------------------------------- helpers
    def _build_row(
        self,
        reg: RegisterDef,
        *,
        is_pin: bool,
        parent: QWidget,
    ) -> tuple[QWidget, _Row]:
        row_widget = QWidget(parent)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(2, 1, 2, 1)
        row_layout.setSpacing(6)

        prefix = "R" if is_pin else "S"
        label = QLabel(f"<b>{prefix}{reg.sreg}</b> {reg.label}", row_widget)
        label.setToolTip(reg.tooltip)
        label.setMinimumWidth(220)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        editor = _make_editor(reg, row_widget)
        editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        units = QLabel(reg.units or "", row_widget)
        units.setMinimumWidth(40)

        dirty = QLabel("", row_widget)
        dirty.setStyleSheet("color: #c0392b; font-weight: bold;")
        dirty.setMinimumWidth(12)

        row_layout.addWidget(label)
        row_layout.addWidget(editor, 1)
        row_layout.addWidget(units)
        row_layout.addWidget(dirty)

        # Wire up the editor so the parent panel learns about edits and so
        # focus events bubble up as "this panel is active".
        row = _Row(reg.sreg, is_pin, reg, label, editor, dirty)
        self._wire_editor(row)
        # Track interaction by hooking focus events on the editor.
        editor.installEventFilter(_FocusReporter(self))

        return row_widget, row

    def _wire_editor(self, row: _Row) -> None:
        sreg = row.sreg
        is_pin = row.is_pin

        def emit_changed(*_args: object) -> None:
            self._mark_dirty(sreg, is_pin)
            self.value_changed.emit(sreg, is_pin)

        if isinstance(row.editor, QSpinBox):
            row.editor.valueChanged.connect(emit_changed)
        elif isinstance(row.editor, QComboBox):
            row.editor.currentIndexChanged.connect(emit_changed)

    # ---------------------------------------------------------------- public
    def rows(self) -> dict[tuple[int, bool], _Row]:
        return self._rows

    def dirty(self) -> set[tuple[int, bool]]:
        return set(self._dirty)

    def is_remote(self) -> bool:
        return self._is_remote

    def apply_values(
        self,
        s_params: dict[int, int],
        pin_params: dict[int, int] | None = None,
    ) -> None:
        for sreg, value in s_params.items():
            row = self._rows.get((sreg, False))
            if row is not None:
                row.set_value(value)
                self._mark_clean(sreg, False)
        if pin_params:
            for sreg, value in pin_params.items():
                row = self._rows.get((sreg, True))
                if row is not None:
                    row.set_value(value)
                    self._mark_clean(sreg, True)

    def mark_all_clean(self) -> None:
        for key in list(self._dirty):
            self._mark_clean(key[0], key[1])

    def mark_dirty(self, sreg: int, is_pin: bool) -> None:
        self._mark_dirty(sreg, is_pin)

    def mark_clean(self, sreg: int, is_pin: bool) -> None:
        self._mark_clean(sreg, is_pin)

    def restore_defaults(self) -> None:
        for (sreg, is_pin), row in self._rows.items():
            if row.reg.read_only:
                continue
            if row.reg.default is None:
                continue
            row.set_value(int(row.reg.default))
            self._mark_dirty(sreg, is_pin)

    def get_value(self, sreg: int, is_pin: bool = False) -> int | None:
        row = self._rows.get((sreg, is_pin))
        return None if row is None else row.value()

    def set_value(self, sreg: int, value: int, is_pin: bool = False) -> None:
        row = self._rows.get((sreg, is_pin))
        if row is None:
            return
        row.set_value(value)
        self._mark_dirty(sreg, is_pin)

    # ---------------------------------------------------------------- private
    def _mark_dirty(self, sreg: int, is_pin: bool) -> None:
        key = (sreg, is_pin)
        row = self._rows.get(key)
        if row is None:
            return
        self._dirty.add(key)
        row.dirty_marker.setText("*")
        row.editor.setStyleSheet(_DIRTY_QSS)

    def _mark_clean(self, sreg: int, is_pin: bool) -> None:
        key = (sreg, is_pin)
        self._dirty.discard(key)
        row = self._rows.get(key)
        if row is None:
            return
        row.dirty_marker.setText("")
        row.editor.setStyleSheet("")


class _FocusReporter(QWidget):
    """Tiny event filter that re-emits focus-in as the panel's `interacted`."""

    def __init__(self, panel: "_RegisterPanel") -> None:
        super().__init__(panel)
        self._panel = panel

    def eventFilter(self, _obj: object, event: object) -> bool:  # type: ignore[override]
        # Qt's FocusIn event type number; importing QEvent here keeps the
        # public surface tidy.
        from PySide6.QtCore import QEvent

        if isinstance(event, QEvent) and event.type() == QEvent.Type.FocusIn:
            self._panel.interacted.emit()
        return False


class SettingsTab(QWidget):
    """Two-panel S-register editor for local + remote radios."""

    status_message = Signal(str, int)  # text, timeout_ms

    def __init__(self, radio: Radio, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._radio = radio
        self._state: str = Radio.STATE_DISCONNECTED

        # Sequenced "Load Settings" state: after the local read succeeds we
        # fire the remote read in the params_loaded handler.
        self._load_phase: Optional[str] = None  # None | "local" | "remote"

        # Multi-write tracking for "Save Settings".
        self._save_pending: set[tuple[int, bool, bool]] = set()  # (sreg, is_remote, is_pin)
        self._save_failures: int = 0

        # EEPROM save tracking.
        self._eeprom_pending: set[bool] = set()  # set of is_remote flags

        # Track the panel the user last interacted with — drives "Restore Defaults".
        self._active_panel: Optional[_RegisterPanel] = None

        self._build_ui()
        self._wire_radio()
        self._set_buttons_enabled(False)

    # ---------------------------------------------------------------- UI build
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ---- action button row ---------------------------------------
        actions = QHBoxLayout()
        actions.setSpacing(6)

        self._btn_load = QPushButton("Load Settings", self)
        self._btn_save = QPushButton("Save Settings", self)
        self._btn_copy = QPushButton("Copy → Remote", self)
        self._btn_restore = QPushButton("Restore Defaults", self)
        self._btn_eeprom = QPushButton("Save EEPROM", self)
        self._btn_reboot = QPushButton("Reboot", self)
        self._btn_factory = QPushButton("Factory Reset", self)

        self._btn_profiles = QToolButton(self)
        self._btn_profiles.setText("Profiles")
        self._btn_profiles.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._profiles_menu = QMenu(self._btn_profiles)
        self._populate_profiles_menu()
        self._btn_profiles.setMenu(self._profiles_menu)

        for btn in (
            self._btn_load,
            self._btn_save,
            self._btn_copy,
            self._btn_restore,
            self._btn_eeprom,
            self._btn_reboot,
            self._btn_factory,
        ):
            actions.addWidget(btn)
        actions.addWidget(self._btn_profiles)
        actions.addStretch(1)
        root.addLayout(actions)

        # ---- two register panels -------------------------------------
        panels = QHBoxLayout()
        panels.setSpacing(6)
        self._local_panel = _RegisterPanel("LOCAL", is_remote=False, parent=self)
        self._remote_panel = _RegisterPanel("REMOTE", is_remote=True, parent=self)
        panels.addWidget(self._local_panel, 1)
        panels.addWidget(self._remote_panel, 1)
        root.addLayout(panels, 1)

        # ---- status line ---------------------------------------------
        self._status = QLabel("", self)
        self._status.setStyleSheet("color: #555;")
        root.addWidget(self._status)

        # ---- panel interaction tracking ------------------------------
        self._local_panel.interacted.connect(lambda: self._set_active_panel(self._local_panel))
        self._remote_panel.interacted.connect(lambda: self._set_active_panel(self._remote_panel))
        self._active_panel = self._local_panel

        # ---- button wiring -------------------------------------------
        self._btn_load.clicked.connect(self._on_load_clicked)
        self._btn_save.clicked.connect(self._on_save_clicked)
        self._btn_copy.clicked.connect(self._on_copy_to_remote_clicked)
        self._btn_restore.clicked.connect(self._on_restore_defaults_clicked)
        self._btn_eeprom.clicked.connect(self._on_save_eeprom_clicked)
        self._btn_reboot.clicked.connect(self._on_reboot_clicked)
        self._btn_factory.clicked.connect(self._on_factory_reset_clicked)

    def _populate_profiles_menu(self) -> None:
        self._profiles_menu.clear()
        for preset in BUILT_IN_PRESETS:
            act = QAction(preset.name, self._profiles_menu)
            act.setToolTip(preset.description)
            # default arg pin captures the current preset by value.
            act.triggered.connect(lambda _checked=False, p=preset: self._apply_profile(p))
            self._profiles_menu.addAction(act)

        self._profiles_menu.addSeparator()
        load_act = QAction("Load profile from JSON…", self._profiles_menu)
        load_act.triggered.connect(self._on_profile_load)
        self._profiles_menu.addAction(load_act)

        save_act = QAction("Save current local panel as profile…", self._profiles_menu)
        save_act.triggered.connect(self._on_profile_save)
        self._profiles_menu.addAction(save_act)

    # ---------------------------------------------------------------- radio wiring
    def _wire_radio(self) -> None:
        self._radio.state_changed.connect(self._on_state_changed)
        self._radio.params_loaded.connect(self._on_params_loaded)
        self._radio.write_result.connect(self._on_write_result)
        self._radio.eeprom_saved.connect(self._on_eeprom_saved)
        self._radio.factory_reset_done.connect(self._on_factory_reset_done)
        self._radio.error.connect(self._on_radio_error)

    # ---------------------------------------------------------------- state handling
    def _on_state_changed(self, state: str) -> None:
        self._state = state
        self._set_buttons_enabled(state in _LIVE_STATES)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in (
            self._btn_load,
            self._btn_save,
            self._btn_copy,
            self._btn_restore,
            self._btn_eeprom,
            self._btn_reboot,
            self._btn_factory,
            self._btn_profiles,
        ):
            btn.setEnabled(enabled)

    def _set_active_panel(self, panel: _RegisterPanel) -> None:
        self._active_panel = panel

    def _emit_status(self, text: str, timeout_ms: int = 5000) -> None:
        self._status.setText(text)
        self.status_message.emit(text, timeout_ms)

    # ---------------------------------------------------------------- actions
    def _on_load_clicked(self) -> None:
        # Sequence: read local, then on success read remote (driven by the
        # params_loaded handler — see _on_params_loaded).
        self._load_phase = "local"
        self._emit_status("Loading local settings…", 3000)
        self._radio.read_params(False)

    def _on_save_clicked(self) -> None:
        self._save_pending.clear()
        self._save_failures = 0
        skipped: list[str] = []
        # Batch local and remote writes into one round-trip each — staying in
        # command mode for the whole set is dramatically faster than doing
        # one +++/ATO bracket per register, and also avoids serial races.
        local_batch: list[tuple[int, int, bool, bool]] = []
        remote_batch: list[tuple[int, int, bool, bool]] = []

        for panel in (self._local_panel, self._remote_panel):
            is_remote = panel.is_remote()
            for sreg, is_pin in sorted(panel.dirty()):
                row = panel.rows()[(sreg, is_pin)]
                value = row.value()
                ok, reason = validate(sreg, value, pin=is_pin)
                if not ok:
                    skipped.append(reason)
                    continue
                self._save_pending.add((sreg, is_remote, is_pin))
                (remote_batch if is_remote else local_batch).append(
                    (sreg, value, is_remote, is_pin)
                )

        if skipped:
            for reason in skipped:
                self._emit_status(reason, 6000)

        attempted = len(local_batch) + len(remote_batch)
        if attempted == 0:
            if not skipped:
                self._emit_status("No changes to save.", 4000)
            return

        if local_batch:
            self._radio.write_params_batch(local_batch)
        if remote_batch:
            self._radio.write_params_batch(remote_batch)
        self._emit_status(f"Saving {attempted} register(s)…", 3000)

    def _on_copy_to_remote_clicked(self) -> None:
        copied = 0
        for sreg in sorted(REGISTERS.keys()):
            reg = REGISTERS[sreg]
            if reg.read_only:
                continue
            local_val = self._local_panel.get_value(sreg, is_pin=False)
            if local_val is None:
                continue
            remote_val = self._remote_panel.get_value(sreg, is_pin=False)
            if remote_val == local_val:
                continue
            self._remote_panel.set_value(sreg, local_val, is_pin=False)
            copied += 1
        if copied:
            self._emit_status(f"Copied {copied} register(s) to remote panel (unsaved).", 5000)
        else:
            self._emit_status("Remote already matches local.", 4000)

    def _on_restore_defaults_clicked(self) -> None:
        panel = self._active_panel or self._local_panel
        panel.restore_defaults()
        side = "remote" if panel.is_remote() else "local"
        self._emit_status(f"Restored defaults on {side} panel (unsaved).", 5000)

    def _on_save_eeprom_clicked(self) -> None:
        ans = QMessageBox.question(
            self,
            "Save to EEPROM",
            "Save current settings to EEPROM (AT&W) on local AND remote? "
            "This is permanent until next AT&F.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._eeprom_pending = {False, True}
        self._emit_status("Saving EEPROM on local + remote…", 3000)
        self._radio.save_eeprom(False)
        self._radio.save_eeprom(True)

    def _on_reboot_clicked(self) -> None:
        ans = QMessageBox.question(
            self,
            "Reboot local radio",
            "Reboot the LOCAL radio now? The link will drop momentarily.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._radio.reboot(False)
        self._emit_status("Rebooting local radio…", 3000)

        ans2 = QMessageBox.question(
            self,
            "Reboot remote radio",
            "Also reboot the REMOTE radio?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans2 == QMessageBox.StandardButton.Yes:
            self._radio.reboot(True)
            self._emit_status("Rebooting remote radio…", 3000)

    def _on_factory_reset_clicked(self) -> None:
        ans = QMessageBox.warning(
            self,
            "Factory reset",
            "This erases ALL settings (AT&F) — frequencies, NETID, baud — "
            "you may lose connection until you reconfigure. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._radio.factory_reset(False)
        self._emit_status("Factory reset issued to local radio…", 4000)

    # ---------------------------------------------------------------- profile actions
    def _apply_profile(self, profile: Profile) -> None:
        applied = 0
        for sreg, value in profile.s_registers.items():
            reg = REGISTERS.get(int(sreg))
            if reg is None or reg.read_only:
                continue
            self._local_panel.set_value(int(sreg), int(value), is_pin=False)
            applied += 1
        for sreg, value in profile.pin_registers.items():
            if int(sreg) in PIN_REGISTERS:
                self._local_panel.set_value(int(sreg), int(value), is_pin=True)
                applied += 1
        self._emit_status(
            f"Applied profile '{profile.name}' to local panel ({applied} registers, unsaved).",
            6000,
        )

    def _on_profile_load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load profile",
            "",
            "JSON profiles (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            profile = load_profile(path)
        except Exception as e:
            QMessageBox.critical(self, "Load profile failed", str(e))
            self._emit_status(f"Load profile failed: {e}", 6000)
            return
        self._apply_profile(profile)

    def _on_profile_save(self) -> None:
        name, ok = QInputDialog.getText(self, "Profile name", "Profile name:")
        if not ok or not name.strip():
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save profile",
            f"{name}.json",
            "JSON profiles (*.json);;All files (*)",
        )
        if not path:
            return

        s_regs: dict[int, int] = {}
        for sreg in sorted(REGISTERS.keys()):
            reg = REGISTERS[sreg]
            if reg.read_only:
                continue
            val = self._local_panel.get_value(sreg, is_pin=False)
            if val is not None:
                s_regs[sreg] = int(val)

        pin_regs: dict[int, int] = {}
        for sreg in sorted(PIN_REGISTERS.keys()):
            val = self._local_panel.get_value(sreg, is_pin=True)
            if val is not None:
                pin_regs[sreg] = int(val)

        profile = Profile(
            name=name.strip(),
            description="",
            s_registers=s_regs,
            pin_registers=pin_regs,
        )
        try:
            save_profile(path, profile)
        except Exception as e:
            QMessageBox.critical(self, "Save profile failed", str(e))
            self._emit_status(f"Save profile failed: {e}", 6000)
            return
        self._emit_status(f"Saved profile '{name}' to {path}", 5000)

    # ---------------------------------------------------------------- radio signals
    def _on_params_loaded(self, result: object, is_remote: bool) -> None:
        s_params: dict[int, int] = getattr(result, "s_params", {}) or {}
        pin_params: dict[int, int] = getattr(result, "pin_params", {}) or {}

        panel = self._remote_panel if is_remote else self._local_panel
        panel.apply_values(s_params, pin_params)
        panel.mark_all_clean()

        side = "remote" if is_remote else "local"
        self._emit_status(f"Loaded {side} settings ({len(s_params)} registers)", 5000)

        # Sequenced load: kick off the remote phase only after local succeeded
        # for THIS load operation.
        if self._load_phase == "local" and not is_remote:
            self._load_phase = "remote"
            self._emit_status("Loading remote settings…", 3000)
            self._radio.read_params(True)
        elif self._load_phase == "remote" and is_remote:
            self._load_phase = None

    def _on_write_result(self, sreg: int, value: int, ok: bool, is_remote: bool) -> None:
        # Match against either pin or s-register pending entry.
        keys = [
            (sreg, is_remote, False),
            (sreg, is_remote, True),
        ]
        is_pin = False
        matched = False
        for k in keys:
            if k in self._save_pending:
                self._save_pending.discard(k)
                is_pin = k[2]
                matched = True
                break

        panel = self._remote_panel if is_remote else self._local_panel
        prefix = "R" if is_pin else "S"
        if ok:
            panel.mark_clean(sreg, is_pin)
        else:
            self._save_failures += 1
            self._emit_status(f"Write failed: {prefix}{sreg} = {value}", 6000)

        if matched and not self._save_pending:
            if self._save_failures == 0:
                self._emit_status("All changes saved.", 5000)
            else:
                self._emit_status(
                    f"Save complete with {self._save_failures} failure(s).",
                    7000,
                )
            self._save_failures = 0

    def _on_eeprom_saved(self, ok: bool, is_remote: bool) -> None:
        side = "remote" if is_remote else "local"
        if not ok:
            self._emit_status(f"EEPROM save failed on {side}.", 6000)
        else:
            self._emit_status(f"EEPROM saved on {side}.", 5000)
        self._eeprom_pending.discard(is_remote)
        if not self._eeprom_pending and ok:
            self._emit_status("EEPROM save complete.", 5000)

    def _on_factory_reset_done(self, ok: bool, is_remote: bool) -> None:
        side = "remote" if is_remote else "local"
        if ok:
            self._emit_status(f"Factory reset on {side} succeeded.", 6000)
        else:
            self._emit_status(f"Factory reset on {side} failed.", 6000)

    def _on_radio_error(self, msg: str) -> None:
        self._emit_status(f"Radio error: {msg}", 7000)
        # An error during a load aborts the sequence so the user can retry.
        self._load_phase = None


__all__ = ["SettingsTab"]


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    app = QApplication.instance() or QApplication(sys.argv)
    radio = Radio()
    w = SettingsTab(radio)
    w.resize(1100, 700)
    w.show()
    sys.exit(app.exec())
