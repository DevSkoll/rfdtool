"""State-machine tests for ApplyPersistController.

Drives the controller against a Qt-signal stub Radio so we can deterministically
fire write_batch_done / eeprom_saved / state_changed / params_loaded in the
order the real Radio would, and verify the controller responds correctly.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from rfd.protocol import Ati5Result
from ui.apply_persist import ApplyPersistController, ApplyPersistReport


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class StubRadio(QObject):
    """Minimal Radio stand-in that exposes only the signals + slots
    ApplyPersistController consumes."""

    write_batch_done = Signal(object)
    eeprom_saved = Signal(bool, bool)
    state_changed = Signal(str)
    params_loaded = Signal(object, bool)
    error = Signal(str)
    log = Signal(str, int)

    def __init__(self) -> None:
        super().__init__()
        # Behavioural toggles for tests
        self._reject_at_write: set[int] = set()    # sregs that come back ok=False
        self._silent_change: dict[int, int] = {}    # sreg -> what radio actually has after reboot
        self._fail_eeprom = False
        self._skip_reboot_state_change = False
        self._raise_error_on: str = ""              # "write", "save", "reboot", "verify"
        self.calls: list[str] = []

    # Slots invoked by the controller
    def write_params_batch(self, updates: list[tuple[int, int, bool, bool]]) -> None:
        self.calls.append("write_params_batch")
        if self._raise_error_on == "write":
            QTimer.singleShot(0, lambda: self.error.emit("synthetic write error"))
            return
        results = []
        for s, v, r, p in updates:
            ok = s not in self._reject_at_write
            results.append((s, v, r, p, ok))
        QTimer.singleShot(0, lambda res=results: self.write_batch_done.emit(res))

    def save_eeprom(self, is_remote: bool) -> None:
        self.calls.append(f"save_eeprom({is_remote})")
        if self._raise_error_on == "save":
            QTimer.singleShot(0, lambda: self.error.emit("synthetic save error"))
            return
        ok = not self._fail_eeprom
        QTimer.singleShot(0, lambda: self.eeprom_saved.emit(ok, is_remote))

    def reboot(self, is_remote: bool) -> None:
        self.calls.append(f"reboot({is_remote})")
        if self._raise_error_on == "reboot":
            QTimer.singleShot(0, lambda: self.error.emit("synthetic reboot error"))
            return
        if self._skip_reboot_state_change:
            return
        QTimer.singleShot(0, lambda: self.state_changed.emit("data"))

    def read_params(self, is_remote: bool) -> None:
        self.calls.append(f"read_params({is_remote})")
        if self._raise_error_on == "verify":
            QTimer.singleShot(0, lambda: self.error.emit("synthetic verify error"))
            return
        # Build what the radio "actually has" after reboot.  By default,
        # everything matches the most recent write_params_batch input.
        # If `_silent_change` is set, those sregs report a different value.
        applied = dict(self._last_intended)
        applied.update(self._silent_change)
        s_params = applied
        s_names = {s: f"S{s}" for s in s_params.keys()}
        result = Ati5Result(
            s_params=s_params, pin_params={},
            s_names=s_names, pin_names={},
        )
        QTimer.singleShot(0, lambda r=result: self.params_loaded.emit(r, is_remote))

    # Test convenience: remember the last batched intended values so the
    # default verify response can echo them back.
    _last_intended: dict[int, int] = {}


@pytest.fixture
def stub_radio(qapp):
    return StubRadio()


@pytest.fixture
def controller(qapp, stub_radio):
    return ApplyPersistController(stub_radio)


def _wait_for(qtbot_or_qapp, predicate, timeout_s: float = 3.0):
    """Spin the event loop until predicate() is true or we hit timeout."""
    import time
    deadline = time.monotonic() + timeout_s
    app = QApplication.instance()
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    return predicate()


# ---------------------------------------------------------------- happy path
def test_happy_path_completes_with_full_acceptance(qapp, stub_radio, controller):
    intended = {3: 41, 4: 20, 14: 131}
    stub_radio._last_intended = dict(intended)

    completed: list[ApplyPersistReport] = []
    failed: list[str] = []
    controller.completed.connect(lambda r: completed.append(r))
    controller.failed.connect(lambda m: failed.append(m))

    controller.start(intended)
    assert _wait_for(qapp, lambda: completed or failed)
    assert not failed
    assert len(completed) == 1
    rpt = completed[0]
    assert rpt.intended == intended
    assert dict(rpt.final) == intended
    assert sorted(rpt.accepted) == sorted(intended.items())
    assert rpt.rejected == ()
    assert rpt.eeprom_saved_ok is True
    assert rpt.rebooted is True
    assert rpt.verified is True
    assert rpt.all_applied is True
    assert "write_params_batch" in stub_radio.calls
    assert any(c.startswith("save_eeprom") for c in stub_radio.calls)
    assert any(c.startswith("reboot") for c in stub_radio.calls)
    assert any(c.startswith("read_params") for c in stub_radio.calls)


# ---------------------------------------------------------------- silent rejection
def test_verify_catches_silently_rejected_writes(qapp, stub_radio, controller):
    """Firmware-locked SKUs ACK the write but the post-reboot value is
    unchanged — controller must classify those as rejected."""
    intended = {3: 41, 9: 928000, 10: 50}
    stub_radio._last_intended = dict(intended)
    # Radio "lies" — write says OK but actual value after reboot is the locked one
    stub_radio._silent_change = {9: 915000, 10: 51}

    completed: list[ApplyPersistReport] = []
    controller.completed.connect(lambda r: completed.append(r))
    controller.start(intended, firmware_banner="RFD SiK 3.57 on RFD900X2-US")
    assert _wait_for(qapp, lambda: bool(completed))
    rpt = completed[0]

    assert dict(rpt.accepted) == {3: 41}
    rejected_dict = {s: (i, a) for (s, i, a) in rpt.rejected}
    assert rejected_dict == {9: (928000, 915000), 10: (50, 51)}
    # Both rejected sregs are in the RFD900X2-US lockdown set, so they're
    # listed under `locked` for the dialog's friendly message.
    assert set(rpt.locked) == {9, 10}
    assert rpt.all_applied is False


def test_locked_empty_when_banner_unknown(qapp, stub_radio, controller):
    intended = {9: 928000}
    stub_radio._last_intended = dict(intended)
    stub_radio._silent_change = {9: 915000}
    completed: list[ApplyPersistReport] = []
    controller.completed.connect(lambda r: completed.append(r))
    controller.start(intended)  # no firmware_banner
    assert _wait_for(qapp, lambda: bool(completed))
    rpt = completed[0]
    assert rpt.rejected == ((9, 928000, 915000),)
    assert rpt.locked == ()


# ---------------------------------------------------------------- write-time rejection
def test_write_time_rejection_does_not_abort(qapp, stub_radio, controller):
    """Even if some writes ACK as rejected at command time, the controller
    still completes the EEPROM/reboot/verify cycle and surfaces the truth
    via the verify diff."""
    intended = {3: 41, 9: 928000}
    stub_radio._last_intended = {3: 41}        # only S3 actually got applied
    stub_radio._reject_at_write = {9}
    completed: list[ApplyPersistReport] = []
    controller.completed.connect(lambda r: completed.append(r))
    controller.start(intended)
    assert _wait_for(qapp, lambda: bool(completed))
    rpt = completed[0]
    assert dict(rpt.accepted) == {3: 41}
    rejected = {s for (s, _i, _a) in rpt.rejected}
    assert rejected == {9}


# ---------------------------------------------------------------- failure paths
def test_eeprom_save_failure(qapp, stub_radio, controller):
    intended = {3: 41}
    stub_radio._last_intended = dict(intended)
    stub_radio._fail_eeprom = True
    failed: list[str] = []
    controller.failed.connect(lambda m: failed.append(m))
    controller.start(intended)
    assert _wait_for(qapp, lambda: bool(failed))
    assert "EEPROM" in failed[0]


def test_reboot_timeout(qapp, stub_radio, controller):
    intended = {3: 41}
    stub_radio._last_intended = dict(intended)
    stub_radio._skip_reboot_state_change = True
    # Patch the controller's reboot timeout to something fast for the test.
    import ui.apply_persist as ap
    orig = ap.TIMEOUT_REBOOT
    ap.TIMEOUT_REBOOT = 0.2
    try:
        failed: list[str] = []
        controller.failed.connect(lambda m: failed.append(m))
        controller.start(intended)
        # Need to wait long enough for the watchdog to fire
        assert _wait_for(qapp, lambda: bool(failed), timeout_s=2.0)
        assert "rebooting" in failed[0]
    finally:
        ap.TIMEOUT_REBOOT = orig


def test_radio_error_during_write(qapp, stub_radio, controller):
    intended = {3: 41}
    stub_radio._last_intended = dict(intended)
    stub_radio._raise_error_on = "write"
    failed: list[str] = []
    controller.failed.connect(lambda m: failed.append(m))
    controller.start(intended)
    assert _wait_for(qapp, lambda: bool(failed))
    assert "synthetic write error" in failed[0]


def test_cancel_mid_flight(qapp, stub_radio, controller):
    intended = {3: 41}
    stub_radio._last_intended = dict(intended)
    # Stop the reboot's automatic state_changed so we can cancel during it.
    stub_radio._skip_reboot_state_change = True
    failed: list[str] = []
    controller.failed.connect(lambda m: failed.append(m))
    controller.start(intended)
    # Spin enough for write+save to land and we're now in REBOOTING
    _wait_for(qapp, lambda: controller.state == "rebooting", timeout_s=2.0)
    assert controller.state == "rebooting"
    controller.cancel()
    assert _wait_for(qapp, lambda: bool(failed))
    assert "cancelled" in failed[0]
    assert controller.state == "failed"


# ---------------------------------------------------------------- reentrancy
def test_start_while_running_emits_failure(qapp, stub_radio, controller):
    intended = {3: 41}
    stub_radio._last_intended = dict(intended)
    stub_radio._skip_reboot_state_change = True
    completed: list = []
    failed: list[str] = []
    controller.completed.connect(lambda r: completed.append(r))
    controller.failed.connect(lambda m: failed.append(m))
    controller.start(intended)
    _wait_for(qapp, lambda: controller.state == "rebooting", timeout_s=2.0)
    # Try to start a second time while running.
    controller.start({4: 20})
    assert any("already running" in m for m in failed)


def test_start_with_empty_intended_fails(qapp, stub_radio, controller):
    failed: list[str] = []
    controller.failed.connect(lambda m: failed.append(m))
    controller.start({})
    assert _wait_for(qapp, lambda: bool(failed))
    assert "no parameters" in failed[0]


# ---------------------------------------------------------------- progress signal
def test_progress_signal_walks_all_four_stages(qapp, stub_radio, controller):
    intended = {3: 41}
    stub_radio._last_intended = dict(intended)
    stages: list[tuple[str, int, int]] = []
    controller.progress.connect(
        lambda label, step, total: stages.append((label, step, total))
    )
    completed: list = []
    controller.completed.connect(lambda r: completed.append(r))
    controller.start(intended)
    assert _wait_for(qapp, lambda: bool(completed))
    step_indices = [s[1] for s in stages]
    assert step_indices == [1, 2, 3, 4]
    assert all(t == 4 for _, _, t in stages)
