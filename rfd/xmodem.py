"""XMODEM-CRC sender for the SiK bootloader on RFD900 (8051) radios.

The SiK bootloader speaks XMODEM-CRC over UART when accepting a firmware
image. This module is the SENDER ONLY: we transmit a payload to a receiver
that has signalled its readiness with a 'C' character.

Frame layout (always 128-byte SOH frames; we do NOT use STX/1K):

    SOH | block# | ~block# | <128 data bytes> | CRC16-hi | CRC16-lo

Block numbers start at 1 and are taken mod 256 (so 255 wraps to 0). The
CRC is the XMODEM polynomial (0x1021, init 0x0000) over the 128 data
bytes only. Short final blocks are padded with 0x1A (SUB).

The receiver responds to each block with one of:
    ACK  - block accepted, advance.
    NAK  - block rejected, retransmit.
    CAN  - sent twice in a row means the receiver is aborting.

After the last data block we send EOT and expect an ACK (a single NAK on
EOT triggers a retransmission of the EOT, up to ``retry_limit`` times).
"""
from __future__ import annotations

from typing import Callable, Protocol

# Protocol byte constants. Exposed so tests and callers can reference them
# symbolically rather than sprinkling magic numbers around.
SOH = 0x01
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRC = 0x43  # ASCII 'C' - receiver-side request for CRC mode

_BLOCK_SIZE = 128
_PAD = 0x1A  # SUB, the XMODEM-mandated pad byte for short final blocks


class XmodemError(RuntimeError):
    """Any protocol failure (timeout, too many retries, garbled byte)."""


class XmodemCancelled(XmodemError):
    """Receiver cancelled (CAN CAN) or caller's cancel_check returned True."""


def crc16_xmodem(data: bytes) -> int:
    """XMODEM CRC-16: polynomial 0x1021, initial value 0x0000."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


class _SerialLike(Protocol):
    timeout: float | None

    def read(self, n: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def reset_input_buffer(self) -> None: ...


def _build_block(block_num: int, payload: bytes) -> bytes:
    """Build one SOH frame. ``payload`` must already be 128 bytes."""
    assert len(payload) == _BLOCK_SIZE
    bn = block_num & 0xFF
    crc = crc16_xmodem(payload)
    return bytes([SOH, bn, (~bn) & 0xFF]) + payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def _wait_for_handshake(ser: _SerialLike, timeout: float) -> None:
    """Read until we see 'C' or the deadline expires.

    We use the serial port's own read timeout in small increments so we
    don't depend on monotonic clocks while still bounding the wait.
    """
    import time

    original_timeout = ser.timeout
    # Read in small chunks so callers can stay responsive even with a
    # long handshake_timeout.
    ser.timeout = min(0.1, timeout) if timeout > 0 else 0.0
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            b = ser.read(1)
            if b and b[0] == CRC:
                return
    finally:
        ser.timeout = original_timeout
    raise XmodemError("no handshake from receiver")


def _read_response(ser: _SerialLike, timeout: float) -> int | None:
    """Read one response byte. Returns None on timeout."""
    original_timeout = ser.timeout
    ser.timeout = timeout
    try:
        b = ser.read(1)
    finally:
        ser.timeout = original_timeout
    if not b:
        return None
    return b[0]


def send(
    ser: _SerialLike,
    data: bytes,
    *,
    progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    handshake_timeout: float = 10.0,
    retry_limit: int = 10,
    inter_byte_timeout: float = 1.0,
) -> int:
    """Send ``data`` via XMODEM-CRC. Returns total payload bytes delivered."""
    total = len(data)

    # Drop any boot banner / leftover noise before listening for 'C'.
    ser.reset_input_buffer()
    _wait_for_handshake(ser, handshake_timeout)

    block_num = 1  # XMODEM block numbering starts at 1
    offset = 0
    last_can = False  # True iff the previous response byte was CAN

    while offset < total:
        if cancel_check is not None and cancel_check():
            ser.write(bytes([CAN, CAN]))
            raise XmodemCancelled("cancelled by caller")

        chunk = data[offset:offset + _BLOCK_SIZE]
        if len(chunk) < _BLOCK_SIZE:
            chunk = chunk + bytes([_PAD]) * (_BLOCK_SIZE - len(chunk))
        frame = _build_block(block_num, chunk)

        for _attempt in range(retry_limit):
            ser.write(frame)
            resp = _read_response(ser, inter_byte_timeout)

            if resp == ACK:
                last_can = False
                break
            if resp == CAN:
                if last_can:
                    raise XmodemCancelled("receiver sent CAN CAN")
                last_can = True
                # Treat as a retry - the next byte may be another CAN.
                continue
            # Anything else (NAK, timeout, garbage) -> retry.
            last_can = False
        else:
            raise XmodemError(
                f"block {block_num} not acknowledged after {retry_limit} retries"
            )

        # Block was ACKed. Advance the byte counter by the real payload
        # size (excluding the 0x1A padding on a short final block) so the
        # progress callback always reports user-visible bytes.
        real_bytes = min(_BLOCK_SIZE, total - offset)
        offset += real_bytes
        if progress is not None:
            progress(offset, total)

        block_num = (block_num + 1) & 0xFF

    # End of transmission. Receiver may NAK once (treat as "send EOT
    # again") before finally ACKing.
    last_can = False
    for _attempt in range(retry_limit):
        ser.write(bytes([EOT]))
        resp = _read_response(ser, inter_byte_timeout)
        if resp == ACK:
            return total
        if resp == CAN:
            if last_can:
                raise XmodemCancelled("receiver sent CAN CAN on EOT")
            last_can = True
            continue
        last_can = False
    raise XmodemError(f"EOT not acknowledged after {retry_limit} retries")
