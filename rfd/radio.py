"""Radio driver.

Two layers:

* :class:`RadioCore` — synchronous AT-protocol driver against a duck-typed
  pyserial.Serial.  No Qt.  Fully testable with the MockSerial fixture.
* :class:`Radio` — :class:`QObject` wrapper that owns a RadioCore on a worker
  thread.  All UI interaction is via signals/slots; queued connections move
  work onto the worker thread automatically.

The split exists so unit tests can exercise the protocol layer without a
QApplication, and so threading is a thin shell over a well-tested core.
"""
from __future__ import annotations

import concurrent.futures
import functools
import threading
import time
from typing import Callable, Protocol

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from . import protocol as proto
from .protocol import Ati5Result, RssiReport


class RadioError(RuntimeError):
    """Generic radio-protocol failure."""


class _SerialLike(Protocol):
    timeout: float | None
    is_open: bool
    in_waiting: int

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def flush(self) -> None: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------- RadioCore
class RadioCore:
    """Synchronous AT-protocol driver.

    Methods raise :class:`RadioError` on protocol failure.  The caller is
    responsible for sequencing `enter_command_mode` / `exit_command_mode`
    around any block of AT operations — `Radio` does this automatically.

    The :attr:`lock` is reentrant (so methods can call each other) and is
    held for the duration of any I/O against the serial port.  Threads that
    only want to read passthrough bytes from the port (e.g. the GUI-thread
    read pump) should ``try_acquire(blocking=False)`` and skip the tick when
    the lock is held — that way a long +++ bracket can't have its AT/OK
    response stolen out of the input buffer by a concurrent reader.
    """

    def __init__(self, ser: _SerialLike) -> None:
        self._ser = ser
        self.lock = threading.RLock()

    @property
    def serial(self) -> _SerialLike:
        return self._ser

    # ---------------------------------------- low-level I/O
    def _send_collect(
        self,
        cmd: bytes,
        *,
        timeout: float = 1.0,
        idle_gap: float = 0.15,
    ) -> str:
        """Send `cmd`, then read until either `timeout` elapses or the input
        has been silent for `idle_gap` seconds after at least one byte arrived.
        """
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass
        self._ser.write(cmd)
        try:
            self._ser.flush()
        except Exception:
            pass
        deadline = time.monotonic() + timeout
        last_data = time.monotonic()
        out = bytearray()
        while time.monotonic() < deadline:
            n = self._ser.in_waiting
            chunk = self._ser.read(n if n > 0 else 1)
            if chunk:
                out.extend(chunk)
                last_data = time.monotonic()
            else:
                if out and (time.monotonic() - last_data) >= idle_gap:
                    break
                time.sleep(0.005)
        return out.decode("ascii", errors="replace")

    # ---------------------------------------- command-mode bracket
    def enter_command_mode(
        self,
        bracket: proto.CommandModeBracket | None = None,
    ) -> bool:
        """Send +++ and confirm command mode via three escalating probes.

        SiK 3.x (RFD900X2) sometimes accepts the +++ but doesn't echo OK on
        a bare ``AT`` follow-up. Trying ATI as a fallback catches that case.
        """
        b = bracket or proto.CommandModeBracket()
        with self.lock:
            try:
                self._ser.reset_input_buffer()
            except Exception:
                pass
            time.sleep(b.quiet_before)
            self._ser.write(b.plus_string)
            try:
                self._ser.flush()
            except Exception:
                pass
            time.sleep(b.quiet_after)
            # Probe 1: post-+++ banner. Most SiK builds emit "OK\r\n" here.
            deadline = time.monotonic() + b.reply_timeout
            post_plus = bytearray()
            while time.monotonic() < deadline:
                n = self._ser.in_waiting
                chunk = self._ser.read(n if n else 1)
                if chunk:
                    post_plus.extend(chunk)
                    if b"OK" in post_plus.upper():
                        return True
                else:
                    if post_plus:
                        break
                    time.sleep(0.01)
            # Probe 2: AT.
            if "OK" in self._send_collect(b"AT\r\n", timeout=b.reply_timeout).upper():
                return True
            # Probe 3: ATI — succeeds if the firmware returns its banner,
            # which it does in command mode but never in passthrough.
            reply = self._send_collect(b"ATI\r\n", timeout=b.reply_timeout).upper()
            return any(tok in reply for tok in ("SIK", "RFD", "OK"))

    def exit_command_mode(self) -> bool:
        with self.lock:
            reply = self._send_collect(proto.at_exit_command_mode(), timeout=1.0)
            return "OK" in reply.upper()

    # ---------------------------------------- parameter ops
    def read_params(self, *, remote: bool = False, timeout: float = 3.0) -> Ati5Result:
        with self.lock:
            reply = self._send_collect(proto.at_read_params(remote=remote), timeout=timeout)
            result = proto.parse_ati5(reply)
            if not result.s_params:
                raise RadioError(
                    f"no S-registers in {'remote' if remote else 'local'} ATI5 reply"
                )
            return result

    def write_param(
        self,
        sreg: int,
        value: int,
        *,
        remote: bool = False,
        pin: bool = False,
        timeout: float = 1.0,
    ) -> bool:
        cmd = (
            proto.at_set_pin(sreg, value, remote=remote)
            if pin
            else proto.at_set_param(sreg, value, remote=remote)
        )
        with self.lock:
            reply = self._send_collect(cmd, timeout=timeout)
            return "OK" in reply.upper()

    def write_params_batch(
        self,
        updates: list[tuple[int, int, bool, bool]],
        *,
        timeout: float = 1.0,
    ) -> list[tuple[int, int, bool, bool, bool]]:
        """Run many writes in a single locked block — caller is responsible
        for being in command mode before calling. Returns the same tuples
        with an `ok` flag appended.
        """
        results: list[tuple[int, int, bool, bool, bool]] = []
        with self.lock:
            for sreg, value, is_remote, is_pin in updates:
                cmd = (
                    proto.at_set_pin(sreg, value, remote=is_remote)
                    if is_pin
                    else proto.at_set_param(sreg, value, remote=is_remote)
                )
                reply = self._send_collect(cmd, timeout=timeout)
                results.append((sreg, value, is_remote, is_pin, "OK" in reply.upper()))
        return results

    def save_eeprom(self, *, remote: bool = False) -> bool:
        with self.lock:
            reply = self._send_collect(proto.at_save_eeprom(remote=remote), timeout=2.0)
            return "OK" in reply.upper()

    def reboot(self, *, remote: bool = False) -> None:
        # Reboot disconnects us; no reply is expected.
        with self.lock:
            self._ser.write(proto.at_reboot(remote=remote))
            try:
                self._ser.flush()
            except Exception:
                pass

    def factory_reset(self, *, remote: bool = False) -> bool:
        with self.lock:
            reply = self._send_collect(proto.at_factory_reset(remote=remote), timeout=2.0)
            return "OK" in reply.upper()

    def send_at(self, command: str, *, timeout: float = 1.5) -> str:
        if not command.endswith(("\r", "\n")):
            command = command + "\r\n"
        with self.lock:
            return self._send_collect(
                command.encode("ascii", errors="replace"), timeout=timeout
            )

    def poll_rssi(self, *, timeout: float = 1.0) -> RssiReport:
        with self.lock:
            reply = self._send_collect(proto.at_rssi(), timeout=timeout)
            return proto.parse_ati7(reply)

    def identify(self, *, timeout: float = 1.0) -> dict:
        info: dict[str, object] = {}
        with self.lock:
            info["banner"] = proto.parse_banner(
                self._send_collect(proto.at_identify(), timeout=timeout)
            )
            info["board_id"] = proto.parse_int_response(
                self._send_collect(proto.at_board_id(), timeout=timeout)
            )
            info["board_name"] = proto.board_name(info["board_id"])  # type: ignore[arg-type]
            info["freq_id"] = proto.parse_int_response(
                self._send_collect(proto.at_freq_id(), timeout=timeout)
            )
            info["bootloader_version"] = proto.parse_banner(
                self._send_collect(proto.at_bootloader_version(), timeout=timeout)
            )
        return info

    def enter_bootloader(self) -> None:
        with self.lock:
            self._ser.write(proto.at_bootloader())
            try:
                self._ser.flush()
            except Exception:
                pass


# --------------------------------------------------------------------- async wrapper
def _async(method):
    """Submit `method` to ``self._executor`` instead of running it inline.

    Lets UI code call ``radio.open_port(...)`` from the GUI thread without
    blocking; the actual work happens on the radio's single worker thread.
    Qt signals emitted from that thread are delivered back to the UI via
    queued connections (Qt detects the thread mismatch automatically).
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        return self._executor.submit(method, self, *args, **kwargs)

    return wrapper


# --------------------------------------------------------------------- factory
def default_serial_factory(
    port: str,
    baud: int,
    timeout: float = 0.1,
):
    """Create a real pyserial.Serial.  Tests inject their own factory."""
    import serial  # local import keeps `from rfd import radio` cheap

    return serial.Serial(
        port=port,
        baudrate=baud,
        timeout=timeout,
        write_timeout=2.0,
    )


# --------------------------------------------------------------------- Radio (Qt)
class Radio(QObject):
    """Qt wrapper around RadioCore.  Designed to live on a worker QThread."""

    STATE_DISCONNECTED = "disconnected"
    STATE_DATA = "data"            # passthrough — bytes flow as MAVLink/raw
    STATE_COMMAND = "command"      # +++ bracket open
    STATE_BOOTLOADER = "bootloader"

    # ---- signals (delivered to UI thread via queued connections) ----
    connected = Signal(str, int)            # port, baud
    disconnected = Signal(str)              # reason
    error = Signal(str)
    log = Signal(str, int)                  # text, level (0=info, 1=warn, 2=err)
    state_changed = Signal(str)
    params_loaded = Signal(object, bool)    # Ati5Result, is_remote
    write_result = Signal(int, int, bool, bool)  # sreg, value, ok, is_remote
    write_batch_done = Signal(object)            # list of (sreg, value, is_remote, is_pin, ok)
    rssi_received = Signal(object)          # RssiReport
    rx_data = Signal(bytes)                 # raw passthrough bytes
    radio_info = Signal(object)             # dict
    mavlink_radio_status = Signal(object)   # rfd.mavlink_parser.RadioStatus
    eeprom_saved = Signal(bool, bool)       # ok, is_remote
    factory_reset_done = Signal(bool, bool)
    at_response = Signal(str, str, bool)    # cmd, response, ok

    # Internal signals — emitted from the worker thread, delivered to slots on
    # this object (which lives on the GUI thread) via a queued connection so
    # all QTimer operations run on the right thread.
    _pump_start_requested = Signal()
    _pump_stop_requested = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        serial_factory: Callable[[str, int, float], _SerialLike] | None = None,
        mavlink_parsing: bool = True,
    ) -> None:
        super().__init__(parent)
        self._serial_factory = serial_factory or default_serial_factory
        self._core: RadioCore | None = None
        self._state = self.STATE_DISCONNECTED
        self._mav_parser = None
        self._mavlink_enabled = mavlink_parsing
        self._read_timer: QTimer | None = None
        self._port = ""
        self._baud = 0
        # Single-thread executor serialises all radio I/O onto a worker so the
        # UI thread never blocks on the +++ bracket / serial reads.  Qt signals
        # emitted from this thread are delivered to the UI via queued
        # connections automatically (Qt detects the thread mismatch).
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="rfd-radio",
        )
        # Held while the worker is in command mode so the read pump (which
        # runs on the GUI thread) doesn't race with command-response reads.
        self._serial_lock = threading.Lock()
        # Marshal start/stop of the QTimer onto whichever thread owns this
        # QObject (the GUI thread) — the worker thread cannot create or
        # touch QTimers safely.
        self._pump_start_requested.connect(self._do_start_read_pump)
        self._pump_stop_requested.connect(self._do_stop_read_pump)

    def shutdown(self) -> None:
        """Stop the worker thread and release resources.  Safe to call twice."""
        try:
            self._cleanup()
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # ---------------------------------------- read pump (data mode only)
    def _start_read_pump(self) -> None:
        """Thread-safe — emits a signal that's serviced on the GUI thread."""
        self._pump_start_requested.emit()

    def _stop_read_pump(self) -> None:
        self._pump_stop_requested.emit()

    @Slot()
    def _do_start_read_pump(self) -> None:
        if self._read_timer is None:
            self._read_timer = QTimer(self)
            self._read_timer.setInterval(50)
            self._read_timer.timeout.connect(self._tick_read_pump)
        self._read_timer.start()

    @Slot()
    def _do_stop_read_pump(self) -> None:
        if self._read_timer is not None:
            self._read_timer.stop()

    def _tick_read_pump(self) -> None:
        if self._core is None or self._state != self.STATE_DATA:
            return
        # Cooperative gate: if the worker thread holds the serial lock (because
        # it's in the middle of a +++ bracket or AT exchange) skip this tick.
        # Without this, the GUI thread would steal AT/OK responses out of the
        # input buffer and command-mode entry would intermittently fail.
        if not self._core.lock.acquire(blocking=False):
            return
        try:
            ser = self._core.serial
            n = ser.in_waiting
            if n <= 0:
                return
            data = bytes(ser.read(n))
            if not data:
                return
            self.rx_data.emit(data)
            if self._mavlink_enabled:
                if self._mav_parser is None:
                    from .mavlink_parser import RadioStatusParser
                    self._mav_parser = RadioStatusParser()
                for msg in self._mav_parser.feed(data):
                    self.mavlink_radio_status.emit(msg)
        except Exception as e:
            self.error.emit(f"read pump: {e}")
        finally:
            self._core.lock.release()

    def _set_state(self, s: str) -> None:
        if s != self._state:
            self._state = s
            self.state_changed.emit(s)

    def _ensure_command(self) -> bool:
        if self._core is None:
            self.error.emit("not connected")
            return False
        if self._state == self.STATE_COMMAND:
            return True
        self._stop_read_pump()
        ok = self._core.enter_command_mode()
        if not ok:
            self.error.emit("could not enter command mode")
            self._set_state(self.STATE_DATA)
            self._start_read_pump()
            return False
        self._set_state(self.STATE_COMMAND)
        return True

    def _back_to_data(self) -> None:
        if self._core is None:
            return
        try:
            self._core.exit_command_mode()
        except Exception:
            pass
        self._set_state(self.STATE_DATA)
        self._start_read_pump()

    def _cleanup(self) -> None:
        self._stop_read_pump()
        if self._core is not None:
            try:
                self._core.serial.close()
            except Exception:
                pass
            self._core = None
        self._mav_parser = None
        self._set_state(self.STATE_DISCONNECTED)

    # ---------------------------------------- slots: connection
    @_async
    @Slot(str, int)
    def open_port(self, port: str, baud: int) -> None:
        try:
            ser = self._serial_factory(port, baud, 0.1)
            self._core = RadioCore(ser)
            self._port, self._baud = port, baud
            self.log.emit(f"opened {port} @ {baud}", 0)
        except Exception as e:
            self.error.emit(f"open port failed: {e}")
            self._cleanup()
            return

        if not self._core.enter_command_mode():
            self.log.emit("could not enter command mode (wrong baud or radio in passthrough)", 1)
            self._set_state(self.STATE_DATA)
            self._start_read_pump()
            self.connected.emit(port, baud)
            return
        self._set_state(self.STATE_COMMAND)

        try:
            info = self._core.identify(timeout=1.5)
            self.radio_info.emit(info)
            self.log.emit(f"identified: {info.get('board_name')} / {info.get('banner')}", 0)
        except Exception as e:
            self.log.emit(f"identify failed: {e}", 1)

        try:
            local = self._core.read_params(timeout=3.0)
            self.params_loaded.emit(local, False)
        except Exception as e:
            self.log.emit(f"read local params failed: {e}", 1)

        try:
            remote = self._core.read_params(remote=True, timeout=1.0)
            self.params_loaded.emit(remote, True)
        except Exception:
            self.log.emit("no remote radio response (RTI5 timed out)", 0)

        self._back_to_data()
        self.connected.emit(port, baud)

    @_async
    @Slot()
    def close_port(self) -> None:
        self._cleanup()
        self.disconnected.emit("user requested")

    # ---------------------------------------- slots: parameter ops
    @_async
    @Slot(bool)
    def read_params(self, is_remote: bool = False) -> None:
        if not self._ensure_command():
            return
        try:
            res = self._core.read_params(remote=is_remote, timeout=3.0)  # type: ignore[union-attr]
            self.params_loaded.emit(res, is_remote)
        except Exception as e:
            self.error.emit(f"read params: {e}")
        finally:
            self._back_to_data()

    @_async
    @Slot(int, int, bool, bool)
    def write_param(
        self,
        sreg: int,
        value: int,
        is_remote: bool = False,
        is_pin: bool = False,
    ) -> None:
        if not self._ensure_command():
            return
        try:
            ok = self._core.write_param(  # type: ignore[union-attr]
                sreg, value, remote=is_remote, pin=is_pin
            )
            self.write_result.emit(sreg, value, ok, is_remote)
        except Exception as e:
            self.error.emit(f"write param: {e}")
        finally:
            self._back_to_data()

    @_async
    @Slot(object)
    def write_params_batch(self, updates: list[tuple[int, int, bool, bool]]) -> None:
        """Send many writes in a single command-mode bracket.

        `updates` is a list of (sreg, value, is_remote, is_pin) tuples.  Emits
        an individual `write_result` for each entry plus one `write_batch_done`
        with the full results list at the end.  Stays in command mode until
        the entire batch is processed, so a 27-write save takes one +++/ATO
        round trip instead of 27.
        """
        if not updates:
            return
        if not self._ensure_command():
            return
        try:
            results = self._core.write_params_batch(list(updates))  # type: ignore[union-attr]
            for sreg, value, is_remote, is_pin, ok in results:
                self.write_result.emit(sreg, value, ok, is_remote)
            self.write_batch_done.emit(results)
        except Exception as e:
            self.error.emit(f"batch write: {e}")
        finally:
            self._back_to_data()

    @_async
    @Slot(bool)
    def save_eeprom(self, is_remote: bool = False) -> None:
        if not self._ensure_command():
            return
        try:
            ok = self._core.save_eeprom(remote=is_remote)  # type: ignore[union-attr]
            self.eeprom_saved.emit(ok, is_remote)
        except Exception as e:
            self.error.emit(f"save eeprom: {e}")
        finally:
            self._back_to_data()

    @_async
    @Slot(bool)
    def reboot(self, is_remote: bool = False) -> None:
        if not self._ensure_command():
            return
        try:
            self._core.reboot(remote=is_remote)  # type: ignore[union-attr]
            self.log.emit(f"rebooted {'remote' if is_remote else 'local'} radio", 0)
        except Exception as e:
            self.error.emit(f"reboot: {e}")
        finally:
            # After reboot the radio drops the link briefly; just go back to data.
            self._set_state(self.STATE_DATA)
            self._start_read_pump()

    @_async
    @Slot(bool)
    def factory_reset(self, is_remote: bool = False) -> None:
        if not self._ensure_command():
            return
        try:
            ok = self._core.factory_reset(remote=is_remote)  # type: ignore[union-attr]
            self.factory_reset_done.emit(ok, is_remote)
        except Exception as e:
            self.error.emit(f"factory reset: {e}")
        finally:
            self._back_to_data()

    @_async
    @Slot()
    def poll_rssi(self) -> None:
        if not self._ensure_command():
            return
        try:
            r = self._core.poll_rssi()  # type: ignore[union-attr]
            self.rssi_received.emit(r)
        except Exception as e:
            self.error.emit(f"poll rssi: {e}")
        finally:
            self._back_to_data()

    @_async
    @Slot(str)
    def send_raw_at(self, command: str) -> None:
        """Used by the terminal tab.  Stays in command mode after — the
        terminal explicitly calls back_to_data() when the user is done."""
        if not self._ensure_command():
            return
        try:
            reply = self._core.send_at(command)  # type: ignore[union-attr]
            ok = "ERROR" not in reply.upper()
            self.at_response.emit(command, reply, ok)
        except Exception as e:
            self.error.emit(f"send at: {e}")

    @_async
    @Slot()
    def back_to_data(self) -> None:
        if self._core is not None:
            self._back_to_data()

    @_async
    @Slot()
    def enter_bootloader(self) -> None:
        if not self._ensure_command():
            return
        try:
            self._core.enter_bootloader()  # type: ignore[union-attr]
            self._set_state(self.STATE_BOOTLOADER)
            self.log.emit("entered bootloader (AT&UPDATE)", 0)
        except Exception as e:
            self.error.emit(f"enter bootloader: {e}")
