"""Streaming MAVLink RADIO_STATUS extractor.

The RFD900 injects MAVLink RADIO_STATUS (msgid 109) packets into the local
serial stream so a host can monitor link quality without leaving normal
operating mode. This module hides the framing details behind a small
stateful parser: feed it raw bytes as they arrive off the serial port and it
returns any complete, CRC-valid RADIO_STATUS packets that emerged.

Tolerances:
- Mixed MAVLink v1 (0xFE) and v2 (0xFD) frames in the same stream.
- Arbitrary garbage bytes between frames (radio user data, link noise).
- Frames split across multiple feed() calls.
- Bad CRCs and other malformed frames are dropped silently.
"""

from __future__ import annotations

from dataclasses import dataclass

from pymavlink.dialects.v20 import common as mavlink2


@dataclass(frozen=True)
class RadioStatus:
    rssi: int
    remrssi: int
    txbuf: int
    noise: int
    remnoise: int
    rxerrors: int
    fixed: int


_RADIO_STATUS_MSGID = 109


class RadioStatusParser:
    """Stateful streaming parser. Construct once, feed bytes as they arrive."""

    def __init__(self) -> None:
        self._mav = self._make_mav()

    @staticmethod
    def _make_mav() -> mavlink2.MAVLink:
        mav = mavlink2.MAVLink(None)
        mav.robust_parsing = True
        return mav

    def feed(self, data: bytes) -> list[RadioStatus]:
        """Append ``data`` to the internal buffer, return any RADIO_STATUS
        packets that became complete this call."""
        if not data:
            return []

        try:
            messages = self._mav.parse_buffer(data) or []
        except Exception:
            self._mav = self._make_mav()
            return []

        out: list[RadioStatus] = []
        for msg in messages:
            if msg.get_msgId() != _RADIO_STATUS_MSGID:
                continue
            out.append(
                RadioStatus(
                    rssi=int(msg.rssi),
                    remrssi=int(msg.remrssi),
                    txbuf=int(msg.txbuf),
                    noise=int(msg.noise),
                    remnoise=int(msg.remnoise),
                    rxerrors=int(msg.rxerrors),
                    fixed=int(msg.fixed),
                )
            )
        return out

    def reset(self) -> None:
        """Clear internal buffer (e.g. after disconnect)."""
        self._mav = self._make_mav()
