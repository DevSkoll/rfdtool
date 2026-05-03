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
    QDialog,
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
    CATEGORY_MAXIMIZE,
    CATEGORY_MODEL,
    CATEGORY_REGION,
    CATEGORY_USE_CASE,
    Profile,
    delete_user_profile,
    list_user_profiles,
    load_profile,
    presets_by_category,
    save_profile,
    save_user_profile,
    user_profile_dir,
)
from rfd.radio import Radio
from rfd.registers import (
    CANONICAL_PIN_NAMES,
    CANONICAL_SIK_NAMES,
    PIN_REGISTERS,
    REGISTERS,
    RegisterDef,
    all_pin_registers,
    all_registers,
    derive_def,
    validate,
    validate_value,
)
from rfd.validation import ValidationReport, validate_config

from .validation_dialog import ApplyPresetDialog, SavePresetDialog, ValidationDialog


# Connected states in which the action buttons are usable. Anything else
# (disconnected/bootloader) means the radio cannot service AT commands.
_LIVE_STATES: frozenset[str] = frozenset({Radio.STATE_DATA, Radio.STATE_COMMAND})

# Editor background tints for the various row states. Dirty trumps validation
# tints because un-saved edits matter more than stale validation results.
_DIRTY_QSS = "background-color: #fff4c2;"
_VALIDATION_QSS = {
    "error":   "background-color: #ffd6d6;",
    "warning": "background-color: #ffe4b5;",
    "info":    "background-color: #e0f0ff;",
    "ok":      "background-color: #d4edda;",
}


@dataclass
class _Row:
    """One register row inside a panel."""

    sreg: int
    is_pin: bool
    reg: RegisterDef
    container: QWidget          # the row's outer QWidget — used by rebuild to
                                # delete just this row without touching the
                                # pin-group widget that sits in the same layout
    label: QLabel
    editor: QWidget
    dirty_marker: QLabel
    status_marker: QLabel
    is_dirty: bool = False
    validation_severity: str = ""   # "" | "error" | "warning" | "info" | "ok"
    validation_titles: tuple[str, ...] = ()

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
                v = int(value)
                # Auto-expand the spinbox range if the radio reports a value
                # outside our current bounds (e.g. newer firmware widening a
                # register that used to cap lower). Without this the QSpinBox
                # silently clips and the user sees a stale display value.
                if v < self.editor.minimum():
                    self.editor.setMinimum(v)
                if v > self.editor.maximum():
                    self.editor.setMaximum(v)
                self.editor.setValue(v)
            elif isinstance(self.editor, QComboBox):
                idx = self.editor.findData(int(value))
                if idx < 0:
                    # Unknown enum option — append it so the value round-trips.
                    self.editor.addItem(f"{value} (unknown)", int(value))
                    idx = self.editor.findData(int(value))
                self.editor.setCurrentIndex(idx)
        finally:
            self.editor.blockSignals(False)

    def apply_visual(self) -> None:
        """Refresh the editor's background and the status marker to reflect
        ``is_dirty`` and ``validation_severity``. Dirty wins over validation."""
        if self.is_dirty:
            self.editor.setStyleSheet(_DIRTY_QSS)
            self.dirty_marker.setText("*")
        else:
            self.dirty_marker.setText("")
            qss = _VALIDATION_QSS.get(self.validation_severity, "")
            self.editor.setStyleSheet(qss)
        icon = {"error": "✕", "warning": "⚠", "info": "ⓘ", "ok": "✓"}.get(
            self.validation_severity, ""
        )
        colour = {
            "error": "#c0392b", "warning": "#d68910",
            "info": "#2874a6", "ok": "#27ae60",
        }.get(self.validation_severity, "")
        self.status_marker.setText(icon)
        self.status_marker.setStyleSheet(
            f"color: {colour}; font-weight: bold;" if colour else ""
        )
        if self.validation_titles:
            self.status_marker.setToolTip("\n".join(self.validation_titles))
        else:
            self.status_marker.setToolTip("")


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
        # Per-panel mapping learned from the radio's ATI5 response.
        # Defaults to the canonical SiK layout so a disconnected panel
        # still renders something useful.
        self._sreg_to_name: dict[int, str] = dict(CANONICAL_SIK_NAMES)
        self._pin_sreg_to_name: dict[int, str] = dict(CANONICAL_PIN_NAMES)
        self._name_to_sreg: dict[str, int] = {
            n: s for s, n in self._sreg_to_name.items()
        }
        self._pin_name_to_sreg: dict[str, int] = {
            n: s for s, n in self._pin_sreg_to_name.items()
        }

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
        # Stash the layouts so we can clear+rebuild rows when the radio
        # reports a different parameter layout than canonical SiK.
        self._body = body
        self._body_layout = body_layout

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
        self._pin_inner = pin_inner
        self._pin_layout = pin_layout
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

        status = QLabel("", row_widget)
        status.setMinimumWidth(16)

        row_layout.addWidget(label)
        row_layout.addWidget(editor, 1)
        row_layout.addWidget(units)
        row_layout.addWidget(dirty)
        row_layout.addWidget(status)

        # Wire up the editor so the parent panel learns about edits and so
        # focus events bubble up as "this panel is active".
        row = _Row(
            reg.sreg, is_pin, reg, row_widget,
            label, editor, dirty, status,
        )
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
        *,
        s_names: dict[int, str] | None = None,
        pin_names: dict[int, str] | None = None,
    ) -> None:
        """Update row values from a fresh ATI5 read.

        If ``s_names`` / ``pin_names`` are supplied (typically from
        ``Ati5Result``), the panel rebuilds its rows to match the firmware's
        actual parameter layout — making sure RFDesign 3.x users see
        ``S14: MAX_WINDOW`` instead of the canonical ``S14: RTSCTS``
        labelling.  Existing values are preserved across the rebuild.
        """
        if s_names is not None and s_names != self._sreg_to_name:
            self._rebuild_layout(
                s_names=s_names,
                pin_names=pin_names if pin_names is not None else self._pin_sreg_to_name,
            )
        elif pin_names is not None and pin_names != self._pin_sreg_to_name:
            self._rebuild_layout(
                s_names=self._sreg_to_name,
                pin_names=pin_names,
            )

        for sreg, value in s_params.items():
            row = self._rows.get((sreg, False))
            if row is None and sreg in self._sreg_to_name:
                # Layout already rebuilt above but row missing somehow —
                # add it on the fly. Edge case; shouldn't normally hit.
                continue
            if row is not None:
                row.set_value(value)
                self._mark_clean(sreg, False)
        if pin_params:
            for sreg, value in pin_params.items():
                row = self._rows.get((sreg, True))
                if row is not None:
                    row.set_value(value)
                    self._mark_clean(sreg, True)

    # ---------------------------------------------------------------- layout
    def _rebuild_layout(
        self,
        *,
        s_names: dict[int, str],
        pin_names: dict[int, str],
    ) -> None:
        """Replace the S/R row widgets with ones reflecting `s_names` /
        `pin_names`.  Preserves dirty state where the sreg still exists in
        the new layout.

        Crucially, deletes only the row containers — the pin_group widget
        (which also lives in body_layout) is left in place so the pin
        rows under it don't get cascade-deleted out from under their dict
        references.
        """
        dirty_snapshot: dict[tuple[int, bool], int] = {}
        for key in list(self._dirty):
            row = self._rows.get(key)
            if row is not None:
                dirty_snapshot[key] = row.value()

        # Remove only the row containers we created. body_layout's
        # pin_group + stretch stay; pin_layout has only row containers
        # so emptying it via the dict iteration is safe.
        for row in list(self._rows.values()):
            parent_layout = self._pin_layout if row.is_pin else self._body_layout
            parent_layout.removeWidget(row.container)
            row.container.setParent(None)
            row.container.deleteLater()
        self._rows.clear()
        self._dirty.clear()

        self._sreg_to_name = dict(s_names)
        self._name_to_sreg = {n: s for s, n in self._sreg_to_name.items()}
        self._pin_sreg_to_name = dict(pin_names)
        self._pin_name_to_sreg = {n: s for s, n in self._pin_sreg_to_name.items()}

        # New S-reg rows go BEFORE the pin_group (and stretch) so they
        # render at the top of the panel as expected.
        pin_group_idx = self._body_layout.indexOf(self._pin_group)
        if pin_group_idx < 0:
            pin_group_idx = self._body_layout.count()
        insert_at = pin_group_idx
        for sreg in sorted(self._sreg_to_name.keys()):
            name = self._sreg_to_name[sreg]
            reg = derive_def(sreg, name)
            row_widget, row = self._build_row(reg, is_pin=False, parent=self._body)
            self._body_layout.insertWidget(insert_at, row_widget)
            insert_at += 1
            self._rows[(sreg, False)] = row

        for sreg in sorted(self._pin_sreg_to_name.keys()):
            name = self._pin_sreg_to_name[sreg]
            reg = derive_def(sreg, name)
            reg = RegisterDef(
                sreg=reg.sreg, name=reg.name,
                label=f"Pin R{sreg} {name}" if name != "PIN_FUNC" else f"Pin R{sreg} function",
                tooltip=reg.tooltip, kind=reg.kind,
                minimum=reg.minimum, maximum=reg.maximum, enum=reg.enum,
                default=reg.default, units=reg.units,
                variant_notes=reg.variant_notes, read_only=reg.read_only,
            )
            row_widget, row = self._build_row(reg, is_pin=True, parent=self._pin_inner)
            self._pin_layout.addWidget(row_widget)
            self._rows[(sreg, True)] = row

        for (sreg, is_pin), value in dirty_snapshot.items():
            row = self._rows.get((sreg, is_pin))
            if row is not None:
                row.set_value(value)
                self._mark_dirty(sreg, is_pin)

    def sreg_to_name(self) -> dict[int, str]:
        return dict(self._sreg_to_name)

    def name_to_sreg(self) -> dict[str, int]:
        return dict(self._name_to_sreg)

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
        row.is_dirty = True
        row.apply_visual()

    def _mark_clean(self, sreg: int, is_pin: bool) -> None:
        key = (sreg, is_pin)
        self._dirty.discard(key)
        row = self._rows.get(key)
        if row is None:
            return
        row.is_dirty = False
        row.apply_visual()

    # ---------------------------------------------------------------- validation
    def apply_validation(self, report: ValidationReport) -> None:
        """Tint each row according to the worst issue affecting it.

        Rows with no issues become "ok" green for a few seconds (the parent
        tab can call clear_validation() on a timer to fade them).
        """
        # Worst severity per sreg, with rank order error > warning > info > ok
        rank = {"error": 3, "warning": 2, "info": 1, "ok": 0, "": -1}
        per_sreg = report.issues_by_sreg
        for (sreg, is_pin), row in self._rows.items():
            if is_pin:
                # Pin registers aren't covered by current validation rules.
                row.validation_severity = ""
                row.validation_titles = ()
                row.apply_visual()
                continue
            issues = per_sreg.get(sreg, [])
            if not issues:
                row.validation_severity = "ok"
                row.validation_titles = ()
            else:
                worst = max(issues, key=lambda i: rank.get(i.severity, 0))
                row.validation_severity = worst.severity
                row.validation_titles = tuple(i.title for i in issues)
            row.apply_visual()

    def clear_validation(self) -> None:
        for row in self._rows.values():
            row.validation_severity = ""
            row.validation_titles = ()
            row.apply_visual()


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
        self._save_rejected: list[tuple[str, int, int]] = []  # (label, sreg, value) for regs the radio said ERROR to

        # EEPROM save tracking.
        self._eeprom_pending: set[bool] = set()  # set of is_remote flags

        # Track the panel the user last interacted with — drives "Restore Defaults".
        self._active_panel: Optional[_RegisterPanel] = None

        # Cached board name + ATI banner from radio_info.  Both fed into
        # validate_config: board_name powers model-specific rules, banner
        # powers firmware-lockdown detection (R19).
        self._board_name: str = ""
        self._firmware_banner: str = ""
        # The radio's last-known values.  Populated by params_loaded events;
        # R19 needs this to detect "user wants X but radio is locked at Y".
        self._radio_current_values: dict[int, int] = {}

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
        self._btn_validate = QPushButton("Validate", self)
        self._btn_copy = QPushButton("Copy → Remote", self)
        self._btn_restore = QPushButton("Restore Defaults", self)
        self._btn_eeprom = QPushButton("Save EEPROM", self)
        self._btn_reboot = QPushButton("Reboot", self)
        self._btn_factory = QPushButton("Factory Reset", self)

        self._btn_profiles = QToolButton(self)
        self._btn_profiles.setText("Profiles")
        self._btn_profiles.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._profiles_menu = QMenu(self._btn_profiles)
        # Rebuilt every time the menu is shown so user-preset additions and
        # board-detection updates are always reflected.
        self._profiles_menu.aboutToShow.connect(self._populate_profiles_menu)
        self._populate_profiles_menu()
        self._btn_profiles.setMenu(self._profiles_menu)

        # Compact summary label that shows the last validation tally.
        self._validation_status = QLabel("", self)
        self._validation_status.setStyleSheet("color: #555;")

        for btn in (
            self._btn_load,
            self._btn_save,
            self._btn_validate,
            self._btn_copy,
            self._btn_restore,
            self._btn_eeprom,
            self._btn_reboot,
            self._btn_factory,
        ):
            actions.addWidget(btn)
        actions.addWidget(self._btn_profiles)
        actions.addWidget(self._validation_status)
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
        self._btn_validate.clicked.connect(self._on_validate_clicked)
        self._btn_copy.clicked.connect(self._on_copy_to_remote_clicked)
        self._btn_restore.clicked.connect(self._on_restore_defaults_clicked)
        self._btn_eeprom.clicked.connect(self._on_save_eeprom_clicked)
        self._btn_reboot.clicked.connect(self._on_reboot_clicked)
        self._btn_factory.clicked.connect(self._on_factory_reset_clicked)

    def _populate_profiles_menu(self) -> None:
        """(Re)build the Profiles dropdown.

        Called once at init and again every time the menu is about to show,
        so user-preset additions and board-detection updates appear without
        the user having to restart the app.
        """
        self._profiles_menu.clear()

        category_layout = (
            (CATEGORY_REGION,    "Region & frequency"),
            (CATEGORY_USE_CASE,  "Use case"),
            (CATEGORY_MODEL,     "Model defaults"),
            (CATEGORY_MAXIMIZE,  "Maximize (combos)"),
        )
        for category, label in category_layout:
            sub = self._profiles_menu.addMenu(label)
            for preset in presets_by_category(category):
                self._add_preset_action(sub, preset)

        self._profiles_menu.addSeparator()

        # User presets — refreshed on every menu show.
        user_sub = self._profiles_menu.addMenu("User presets")
        user_presets = list_user_profiles()
        if not user_presets:
            empty = QAction("(no saved presets yet)", user_sub)
            empty.setEnabled(False)
            user_sub.addAction(empty)
        else:
            for preset in user_presets:
                self._add_preset_action(user_sub, preset, is_user=True)
            user_sub.addSeparator()
            manage = QAction("Manage user presets…", user_sub)
            manage.triggered.connect(self._on_manage_user_presets)
            user_sub.addAction(manage)

        save_user_act = QAction("Save current as user preset…", self._profiles_menu)
        save_user_act.triggered.connect(self._on_save_user_preset)
        self._profiles_menu.addAction(save_user_act)

        self._profiles_menu.addSeparator()

        load_act = QAction("Import preset from JSON…", self._profiles_menu)
        load_act.triggered.connect(self._on_profile_load)
        self._profiles_menu.addAction(load_act)

        save_act = QAction("Export current panel to JSON…", self._profiles_menu)
        save_act.triggered.connect(self._on_profile_save)
        self._profiles_menu.addAction(save_act)

    def _add_preset_action(
        self,
        menu: QMenu,
        preset: Profile,
        *,
        is_user: bool = False,
    ) -> None:
        applies = preset.matches_board(self._board_name)
        text = preset.name if applies else f"{preset.name}  (n/a for {self._board_name})"
        act = QAction(text, menu)
        tip = preset.description
        if not applies:
            tip += f"  · This preset isn't tagged for {self._board_name}."
        if preset.notes:
            tip += f"\n\n{preset.notes}"
        act.setToolTip(tip)
        if not applies:
            font = act.font()
            font.setItalic(True)
            act.setFont(font)
        # default arg pins the current preset by value
        act.triggered.connect(lambda _checked=False, p=preset: self._apply_profile(p))
        menu.addAction(act)

    # ---------------------------------------------------------------- radio wiring
    def _wire_radio(self) -> None:
        self._radio.state_changed.connect(self._on_state_changed)
        self._radio.params_loaded.connect(self._on_params_loaded)
        self._radio.write_result.connect(self._on_write_result)
        self._radio.eeprom_saved.connect(self._on_eeprom_saved)
        self._radio.factory_reset_done.connect(self._on_factory_reset_done)
        self._radio.error.connect(self._on_radio_error)
        # Track the connected radio's board name so presets can be filtered
        # by applies_to and validation can apply model-specific rules.
        self._radio.radio_info.connect(self._on_radio_info)

    def _on_radio_info(self, info: object) -> None:
        try:
            self._board_name = str(info.get("board_name") or "")  # type: ignore[union-attr]
            self._firmware_banner = str(info.get("banner") or "")  # type: ignore[union-attr]
        except Exception:
            self._board_name = ""
            self._firmware_banner = ""

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
        # Validate is intentionally available even while disconnected — users
        # may want to sanity-check a JSON profile they just loaded before
        # connecting to a radio.
        self._btn_validate.setEnabled(True)

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
        self._save_rejected.clear()
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
        """Show the confirm-and-diff dialog, then stage values on the local panel."""
        current_values: dict[int, int] = {}
        for (sreg, is_pin) in self._local_panel.rows():
            if is_pin:
                continue
            v = self._local_panel.get_value(sreg, is_pin=False)
            if v is not None:
                current_values[sreg] = int(v)
        dialog = ApplyPresetDialog(
            profile,
            current_values,
            board_name=self._board_name,
            name_to_sreg=self._local_panel.name_to_sreg(),
            sreg_to_name=self._local_panel.sreg_to_name(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_profile_force(profile)

    def _apply_profile_force(self, profile: Profile) -> None:
        # Translate the preset's name-keyed params via the panel's actual
        # firmware mapping. Names absent on this radio (e.g. MANCHESTER on
        # RFDesign 3.x) are silently skipped — the underlying parameter
        # doesn't exist on the connected hardware.
        name_to_sreg = self._local_panel.name_to_sreg()
        target_sregs = profile.to_sregs_for(name_to_sreg)
        applied = 0
        skipped_names = []
        for n in sorted(profile.params):
            if n not in name_to_sreg:
                skipped_names.append(n)
        for sreg, value in target_sregs.items():
            row = self._local_panel.rows().get((int(sreg), False))
            if row is None or row.reg.read_only:
                continue
            self._local_panel.set_value(int(sreg), int(value), is_pin=False)
            applied += 1
        for sreg, value in profile.pin_registers.items():
            row = self._local_panel.rows().get((int(sreg), True))
            if row is not None:
                self._local_panel.set_value(int(sreg), int(value), is_pin=True)
                applied += 1
        msg = (
            f"Applied preset '{profile.name}' to local panel "
            f"({applied} registers, unsaved). Click Save Settings to commit."
        )
        if skipped_names:
            msg += (
                f"  Skipped {len(skipped_names)} parameter(s) not present on "
                f"this radio: {', '.join(skipped_names)}."
            )
        self._emit_status(msg, 8000)

    # ---------------------------------------------------------------- validation
    def _on_validate_clicked(self) -> None:
        s_params: dict[int, int] = {}
        for sreg in REGISTERS.keys():
            v = self._local_panel.get_value(sreg, is_pin=False)
            if v is not None:
                s_params[sreg] = int(v)
        pin_params: dict[int, int] = {}
        for sreg in PIN_REGISTERS.keys():
            v = self._local_panel.get_value(sreg, is_pin=True)
            if v is not None:
                pin_params[sreg] = int(v)

        report = validate_config(
            s_params,
            sreg_to_name=self._local_panel.sreg_to_name(),
            pin_params=pin_params,
            board_name=self._board_name,
            is_remote=False,
            firmware_banner=self._firmware_banner,
            current_values=self._radio_current_values,
        )
        self._local_panel.apply_validation(report)

        # Compact tally next to the action bar.
        if report.overall == "ok":
            self._validation_status.setText(
                "<span style='color:#27ae60;'>✓ all rules pass</span>"
            )
        else:
            parts: list[str] = []
            if report.errors:
                parts.append(f"<span style='color:#c0392b;'>✕ {len(report.errors)}</span>")
            if report.warnings:
                parts.append(f"<span style='color:#d68910;'>⚠ {len(report.warnings)}</span>")
            if report.infos:
                parts.append(f"<span style='color:#2874a6;'>ⓘ {len(report.infos)}</span>")
            self._validation_status.setText(" ".join(parts))
        self._validation_status.setTextFormat(Qt.TextFormat.RichText)

        # Open summary dialog. Hook the "Apply fix" buttons.
        dialog = ValidationDialog(report, parent=self)
        dialog.apply_fix_requested.connect(self._on_apply_validation_fix)
        dialog.exec()

    def _on_apply_validation_fix(self, sreg: int, value: int) -> None:
        self._local_panel.set_value(sreg, int(value), is_pin=False)
        # Mark dirty so the user notices. Validation tints stay until next click.
        self._local_panel.mark_dirty(sreg, is_pin=False)
        self._emit_status(f"Staged fix: S{sreg} = {value} (unsaved)", 5000)

    # ---------------------------------------------------------------- user preset save / manage
    def _on_save_user_preset(self) -> None:
        dialog = SavePresetDialog(
            board_name=self._board_name,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.name()
        if not name:
            QMessageBox.warning(self, "Save preset", "Name cannot be empty.")
            return

        s_regs: dict[int, int] = {}
        for sreg in sorted(REGISTERS.keys()):
            reg = REGISTERS[sreg]
            if reg.read_only:
                continue
            v = self._local_panel.get_value(sreg, is_pin=False)
            if v is not None:
                s_regs[sreg] = int(v)
        pin_regs: dict[int, int] = {}
        for sreg in sorted(PIN_REGISTERS.keys()):
            v = self._local_panel.get_value(sreg, is_pin=True)
            if v is not None:
                pin_regs[sreg] = int(v)

        profile = Profile(
            name=name,
            description=dialog.description(),
            s_registers=s_regs,
            pin_registers=pin_regs,
            applies_to=dialog.applies_to(),
            category="user",
            notes=dialog.notes(),
        )
        try:
            target = save_user_profile(profile)
        except Exception as e:
            QMessageBox.critical(self, "Save preset failed", str(e))
            return
        self._emit_status(f"Saved preset '{name}' to {target}", 5000)

    def _on_manage_user_presets(self) -> None:
        # Minimal manage flow: list + delete via QMessageBox confirms.
        # A richer dialog can replace this later without affecting the API.
        presets = list_user_profiles()
        if not presets:
            QMessageBox.information(self, "User presets", "No saved presets yet.")
            return
        names = "\n".join(f"  • {p.name}" for p in presets)
        msg = QMessageBox(self)
        msg.setWindowTitle("User presets")
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            f"Saved presets in {user_profile_dir()}:\n\n{names}\n\n"
            "Edit or delete files directly in that folder, or use the dialog "
            "below to remove one."
        )
        msg.setStandardButtons(QMessageBox.StandardButton.Close)
        delete_btn = msg.addButton("Delete a preset…", QMessageBox.ButtonRole.ActionRole)
        msg.exec()
        if msg.clickedButton() is delete_btn:
            choices = [p.name for p in presets]
            target_name, ok = QInputDialog.getItem(
                self, "Delete user preset",
                "Select preset to delete:", choices, 0, False,
            )
            if not ok or not target_name:
                return
            target = next((p for p in presets if p.name == target_name), None)
            if target is None:
                return
            if QMessageBox.question(
                self, "Delete preset",
                f"Delete '{target_name}' from {user_profile_dir()}?",
            ) == QMessageBox.StandardButton.Yes:
                if delete_user_profile(target):
                    self._emit_status(f"Deleted preset '{target_name}'", 4000)

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
        # New in v3 of Ati5Result: the firmware's reported parameter names
        # so the panel can render correct labels per radio variant.
        s_names: dict[int, str] = getattr(result, "s_names", {}) or {}
        pin_names: dict[int, str] = getattr(result, "pin_names", {}) or {}

        panel = self._remote_panel if is_remote else self._local_panel
        panel.apply_values(
            s_params,
            pin_params,
            s_names=s_names if s_names else None,
            pin_names=pin_names if pin_names else None,
        )
        panel.mark_all_clean()

        # Cache the LOCAL snapshot so the validator's R19 (firmware lockdown
        # detection) can compare user-intended values against what the radio
        # currently has. We don't bother caching the remote values because
        # R19 is skipped for is_remote=True anyway.
        if not is_remote:
            self._radio_current_values = dict(s_params)

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
            self._save_rejected.append((f"{prefix}{sreg}", sreg, value))

        if matched and not self._save_pending:
            if self._save_failures == 0:
                self._emit_status("All changes saved.", 5000)
            else:
                self._announce_save_failures()
            self._save_failures = 0
            self._save_rejected.clear()

    def _announce_save_failures(self) -> None:
        """Build a single status-bar line naming the rejected registers.

        We deliberately don't pop a modal dialog here — the canonical place
        to learn about firmware-locked registers is the Validate button,
        which previews them *before* the user clicks Save.
        """
        labels = ", ".join(label for label, _sreg, _val in self._save_rejected)
        msg = (
            f"Save complete — {self._save_failures} register(s) rejected by the radio: {labels}. "
            "Click Validate for details."
        )
        self._emit_status(msg, 12000)

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
