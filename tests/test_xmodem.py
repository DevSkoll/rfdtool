"""Tests for the XMODEM-CRC sender."""
from __future__ import annotations

from typing import Callable

import pytest

from rfd.xmodem import (
    ACK,
    CAN,
    CRC,
    EOT,
    NAK,
    SOH,
    XmodemCancelled,
    XmodemError,
    crc16_xmodem,
    send,
)
from tests.conftest import MockSerial


# Each block on the wire is SOH + bn + ~bn + 128 data + 2 CRC = 133 bytes.
_FRAME_LEN = 1 + 1 + 1 + 128 + 2


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _Responder:
    """Synchronous response generator wired via a write-hook on MockSerial.

    The mock's ``read_returns_immediately`` flag makes the real-time threading
    pattern racy: the sender writes a frame and reads back immediately, so
    any responder living on another thread loses the race. Instead, we
    intercept ``write`` and feed the responder's reply directly into the
    read buffer before ``write`` returns.
    """

    def __init__(self, mock: MockSerial,
                 responder: Callable[[bytes], bytes],
                 *, feed_handshake: bool = True) -> None:
        self._mock = mock
        self._responder = responder
        self._buf = bytearray()
        self._orig_write = mock.write
        self._orig_reset = mock.reset_input_buffer
        self._feed_handshake = feed_handshake
        mock.write = self._write  # type: ignore[method-assign]
        # Feed 'C' AFTER reset_input_buffer so it survives the sender's
        # initial drain step.
        mock.reset_input_buffer = self._reset_input_buffer  # type: ignore[method-assign]

    def _reset_input_buffer(self) -> None:
        self._orig_reset()
        if self._feed_handshake:
            self._mock.feed(b"C")

    def _write(self, data: bytes) -> int:
        n = self._orig_write(data)
        self._buf.extend(data)
        # Pull out complete units from the in-progress write stream.
        while self._buf:
            first = self._buf[0]
            if first == SOH:
                if len(self._buf) < _FRAME_LEN:
                    break
                frame = bytes(self._buf[:_FRAME_LEN])
                del self._buf[:_FRAME_LEN]
                resp = self._responder(frame)
                if resp:
                    self._mock.feed(resp)
            elif first == EOT:
                del self._buf[:1]
                resp = self._responder(b"\x04")
                if resp:
                    self._mock.feed(resp)
            elif first == CAN:
                # Sender is aborting; drain and stop responding.
                self._buf.clear()
            else:
                # Unknown leading byte - drop it and keep going.
                del self._buf[:1]
        return n


def _run_with_responder(
    mock_serial: MockSerial,
    data: bytes,
    responder: Callable[[bytes], bytes],
    *,
    handshake_timeout: float = 1.0,
    retry_limit: int = 10,
    progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[int | None, BaseException | None]:
    """Synchronously run ``send`` with a per-frame responder."""
    _Responder(mock_serial, responder)
    try:
        ret = send(
            mock_serial,
            data,
            progress=progress,
            cancel_check=cancel_check,
            handshake_timeout=handshake_timeout,
            retry_limit=retry_limit,
            inter_byte_timeout=0.05,
        )
        return ret, None
    except BaseException as exc:  # noqa: BLE001 - tests inspect type
        return None, exc


# ---------------------------------------------------------------------------
# CRC vectors
# ---------------------------------------------------------------------------

def test_crc16_known_vectors() -> None:
    assert crc16_xmodem(b"123456789") == 0x31C3
    assert crc16_xmodem(b"") == 0x0000
    assert crc16_xmodem(b"A") == 0x58E5
    # All-zero block - identity for the polynomial register.
    assert crc16_xmodem(b"\x00" * 128) == 0x0000


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_two_blocks(mock_serial: MockSerial) -> None:
    payload = bytes(range(200))  # 200 bytes -> 2 blocks, second block padded

    def responder(_frame: bytes) -> bytes:
        return bytes([ACK])  # ACK every block AND the EOT

    progress_calls: list[tuple[int, int]] = []

    def on_progress(sent: int, total: int) -> None:
        progress_calls.append((sent, total))

    ret, exc = _run_with_responder(
        mock_serial, payload, responder, progress=on_progress
    )
    assert exc is None, f"unexpected exception: {exc!r}"
    assert ret == 200

    written = bytes(mock_serial.written)
    assert len(written) == 2 * _FRAME_LEN + 1  # 2 blocks + EOT
    assert written[-1] == EOT

    # Block 1: SOH | 0x01 | 0xFE | <128 bytes> | crc-hi | crc-lo
    assert written[0] == SOH
    assert written[1] == 0x01
    assert written[2] == 0xFE
    assert written[3:131] == bytes(range(128))

    # Block 2 starts at offset _FRAME_LEN.
    b2 = written[_FRAME_LEN:_FRAME_LEN * 2]
    assert b2[0] == SOH
    assert b2[1] == 0x02
    assert b2[2] == 0xFD
    # First 72 bytes are payload[128:200]; remaining 56 are 0x1A padding.
    assert b2[3:3 + 72] == bytes(range(128, 200))
    assert b2[3 + 72:3 + 128] == bytes([0x1A]) * 56

    # Progress: cumulative, never exceeds total, ends at total.
    assert progress_calls[-1] == (200, 200)
    for sent, total in progress_calls:
        assert 0 < sent <= total == 200
    sent_values = [s for s, _ in progress_calls]
    assert sent_values == sorted(sent_values)


def test_progress_caps_at_total(mock_serial: MockSerial) -> None:
    payload = b"X" * 300  # 3 blocks (the third is heavily padded)

    def responder(_frame: bytes) -> bytes:
        return bytes([ACK])

    progress_calls: list[tuple[int, int]] = []
    ret, exc = _run_with_responder(
        mock_serial, payload, responder,
        progress=lambda s, t: progress_calls.append((s, t)),
    )
    assert exc is None
    assert ret == 300
    assert progress_calls[-1] == (300, 300)
    assert all(s <= t for s, t in progress_calls)


# ---------------------------------------------------------------------------
# Block number wrap
# ---------------------------------------------------------------------------

def test_block_number_wrap(mock_serial: MockSerial) -> None:
    # 257 blocks - block 256 must have block# 0, block 257 must have block# 1.
    n_blocks = 257
    payload = b"\xAA" * (n_blocks * 128)

    def responder(_frame: bytes) -> bytes:
        return bytes([ACK])

    ret, exc = _run_with_responder(mock_serial, payload, responder)
    assert exc is None
    assert ret == len(payload)

    written = bytes(mock_serial.written)
    assert written.endswith(bytes([EOT]))

    # Pull the block# field out of every frame.
    block_numbers = []
    for i in range(n_blocks):
        off = i * _FRAME_LEN
        assert written[off] == SOH
        bn = written[off + 1]
        inv = written[off + 2]
        assert (bn ^ inv) == 0xFF, f"block# / ~block# mismatch at frame {i}"
        block_numbers.append(bn)

    # First block is 1, then increments mod 256.
    assert block_numbers[0] == 1
    assert block_numbers[254] == 255
    assert block_numbers[255] == 0   # the wrap
    assert block_numbers[256] == 1


# ---------------------------------------------------------------------------
# NAK retry
# ---------------------------------------------------------------------------

def test_nak_then_ack_retransmits_same_block(mock_serial: MockSerial) -> None:
    payload = b"hello"  # one block
    responses = iter([NAK, ACK, ACK])  # NAK block, ACK retry, ACK EOT

    def responder(_frame: bytes) -> bytes:
        return bytes([next(responses)])

    ret, exc = _run_with_responder(mock_serial, payload, responder)
    assert exc is None
    assert ret == 5

    written = bytes(mock_serial.written)
    # We expect: block (133) + retransmit of same block (133) + EOT (1) = 267
    assert len(written) == _FRAME_LEN * 2 + 1
    first = written[:_FRAME_LEN]
    second = written[_FRAME_LEN:_FRAME_LEN * 2]
    assert first == second  # exact retransmit
    assert written[-1] == EOT


# ---------------------------------------------------------------------------
# Retry limit exceeded
# ---------------------------------------------------------------------------

def test_retry_limit_exceeded_raises(mock_serial: MockSerial) -> None:
    payload = b"hello"

    def responder(_frame: bytes) -> bytes:
        return bytes([NAK])  # always NAK

    ret, exc = _run_with_responder(
        mock_serial, payload, responder, retry_limit=4
    )
    assert ret is None
    assert isinstance(exc, XmodemError)
    assert not isinstance(exc, XmodemCancelled)

    # The same block should have been written exactly retry_limit times.
    written = bytes(mock_serial.written)
    assert len(written) == _FRAME_LEN * 4
    first = written[:_FRAME_LEN]
    for i in range(4):
        assert written[i * _FRAME_LEN:(i + 1) * _FRAME_LEN] == first


# ---------------------------------------------------------------------------
# CAN CAN abort
# ---------------------------------------------------------------------------

def test_can_can_aborts(mock_serial: MockSerial) -> None:
    payload = b"hello"
    # First write to block 1 -> respond CAN, CAN. We feed both at once so
    # the sender consumes them on two consecutive read attempts (the second
    # read will be after a retransmit, since one CAN alone is treated as a
    # retry).
    responses = iter([bytes([CAN]), bytes([CAN]), bytes([ACK])])

    def responder(_frame: bytes) -> bytes:
        try:
            return next(responses)
        except StopIteration:
            return b""

    ret, exc = _run_with_responder(mock_serial, payload, responder)
    assert ret is None
    assert isinstance(exc, XmodemCancelled)


# ---------------------------------------------------------------------------
# cancel_check
# ---------------------------------------------------------------------------

def test_cancel_check_aborts_and_writes_can_can(mock_serial: MockSerial) -> None:
    payload = b"hello"

    cancel_state = {"flag": False}

    def cancel_check() -> bool:
        return cancel_state["flag"]

    def responder(frame: bytes) -> bytes:
        if frame[0] == SOH:
            # Trip the cancel flag right after the first block is ACKed so
            # the next iteration's cancel_check fires before block 2.
            cancel_state["flag"] = True
            return bytes([ACK])
        return bytes([ACK])

    # Need a multi-block payload so cancel_check actually runs again before
    # a second block.
    payload = b"x" * 200

    ret, exc = _run_with_responder(
        mock_serial, payload, responder, cancel_check=cancel_check
    )
    assert ret is None
    assert isinstance(exc, XmodemCancelled)

    written = bytes(mock_serial.written)
    # Should end with two CAN bytes.
    assert written[-2:] == bytes([CAN, CAN])


# ---------------------------------------------------------------------------
# No handshake
# ---------------------------------------------------------------------------

def test_no_handshake_raises(mock_serial: MockSerial) -> None:
    mock_serial.timeout = 0.01
    # Don't feed anything - sender should give up after handshake_timeout.
    with pytest.raises(XmodemError, match="no handshake"):
        send(
            mock_serial,
            b"hello",
            handshake_timeout=0.05,
            inter_byte_timeout=0.01,
        )
