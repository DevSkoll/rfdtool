"""Shared test fixtures.

The MockSerial class quacks like pyserial.Serial enough for the protocol layer,
the XMODEM sender, and the firmware uploader to be exercised without hardware.

Two interaction modes:
  * Manual: tests call `feed(bytes)` to enqueue what the code-under-test will
    read on the next `.read()`, and inspect `.written` afterward.
  * Scripted: tests register `(trigger_substring, response_bytes)` pairs via
    `script(...)`. Whenever a `.write()` payload contains the trigger, the
    response is appended to the read buffer.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

import pytest


class MockSerial:
    """Drop-in replacement for serial.Serial for tests."""

    def __init__(self, port: str = "/dev/ttyMOCK", baudrate: int = 57600,
                 timeout: float | None = 1.0, **_: object) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.write_timeout: float | None = timeout
        self.is_open = True
        self._rx = bytearray()
        self._rx_lock = threading.Lock()
        self.written = bytearray()
        self._scripts: list[tuple[bytes, bytes | Callable[[bytes], bytes]]] = []
        # When True, .read() returns immediately with whatever is buffered
        # rather than waiting on the timeout. Most tests want this.
        self.read_returns_immediately = True

    # ------------------------------------------------------------------ pyserial API
    def read(self, size: int = 1) -> bytes:
        if not self.is_open:
            raise RuntimeError("port closed")
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        out = bytearray()
        while len(out) < size:
            with self._rx_lock:
                take = min(size - len(out), len(self._rx))
                if take:
                    out.extend(self._rx[:take])
                    del self._rx[:take]
            if len(out) >= size:
                break
            if self.read_returns_immediately:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.001)
        return bytes(out)

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        out = bytearray()
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while True:
            with self._rx_lock:
                if self._rx:
                    out.extend(self._rx)
                    self._rx.clear()
            idx = out.find(expected)
            if idx >= 0:
                end = idx + len(expected)
                # push back any extra
                extra = bytes(out[end:])
                if extra:
                    with self._rx_lock:
                        self._rx[:0] = extra
                return bytes(out[:end])
            if size is not None and len(out) >= size:
                return bytes(out[:size])
            if self.read_returns_immediately:
                return bytes(out)
            if deadline is not None and time.monotonic() >= deadline:
                return bytes(out)
            time.sleep(0.001)

    def readline(self) -> bytes:
        return self.read_until(b"\n")

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise RuntimeError("port closed")
        self.written.extend(data)
        # Process scripted responses
        for trigger, response in list(self._scripts):
            if trigger and trigger in data:
                payload = response(data) if callable(response) else response
                if payload:
                    with self._rx_lock:
                        self._rx.extend(payload)
        return len(data)

    @property
    def in_waiting(self) -> int:
        with self._rx_lock:
            return len(self._rx)

    @property
    def out_waiting(self) -> int:
        return 0

    def flush(self) -> None: ...
    def reset_input_buffer(self) -> None:
        with self._rx_lock:
            self._rx.clear()

    def reset_output_buffer(self) -> None:
        self.written.clear()

    def close(self) -> None:
        self.is_open = False

    def open(self) -> None:
        self.is_open = True

    # ------------------------------------------------------------------ test helpers
    def feed(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("ascii")
        with self._rx_lock:
            self._rx.extend(data)

    def script(self, trigger: bytes | str,
               response: bytes | str | Callable[[bytes], bytes]) -> None:
        if isinstance(trigger, str):
            trigger = trigger.encode("ascii")
        if isinstance(response, str):
            response = response.encode("ascii")
        self._scripts.append((trigger, response))

    def clear_written(self) -> None:
        self.written.clear()

    def written_str(self) -> str:
        return self.written.decode("ascii", errors="replace")


@pytest.fixture
def mock_serial() -> MockSerial:
    return MockSerial()
