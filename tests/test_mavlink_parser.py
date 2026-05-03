"""Tests for rfd.mavlink_parser.RadioStatusParser."""

from __future__ import annotations

import pytest
from pymavlink.dialects.v20 import common as ml2

from rfd.mavlink_parser import RadioStatus, RadioStatusParser


def _make_mav(src_system: int = 51, src_component: int = 68) -> ml2.MAVLink:
    mav = ml2.MAVLink(None)
    mav.srcSystem = src_system
    mav.srcComponent = src_component
    return mav


def _radio_status_frame(
    *,
    rssi: int = 200,
    remrssi: int = 190,
    txbuf: int = 100,
    noise: int = 40,
    remnoise: int = 42,
    rxerrors: int = 0,
    fixed: int = 3,
    mav: ml2.MAVLink | None = None,
) -> bytes:
    mav = mav if mav is not None else _make_mav()
    msg = ml2.MAVLink_radio_status_message(
        rssi=rssi,
        remrssi=remrssi,
        txbuf=txbuf,
        noise=noise,
        remnoise=remnoise,
        rxerrors=rxerrors,
        fixed=fixed,
    )
    return msg.pack(mav)


def _heartbeat_frame(mav: ml2.MAVLink | None = None) -> bytes:
    mav = mav if mav is not None else _make_mav()
    msg = ml2.MAVLink_heartbeat_message(
        type=1,
        autopilot=0,
        base_mode=0,
        custom_mode=0,
        system_status=0,
        mavlink_version=3,
    )
    return msg.pack(mav)


def test_single_v2_frame_returns_one_radio_status() -> None:
    parser = RadioStatusParser()
    frame = _radio_status_frame(
        rssi=200, remrssi=190, txbuf=100, noise=40, remnoise=42, rxerrors=7, fixed=3
    )

    result = parser.feed(frame)

    assert result == [
        RadioStatus(
            rssi=200,
            remrssi=190,
            txbuf=100,
            noise=40,
            remnoise=42,
            rxerrors=7,
            fixed=3,
        )
    ]


def test_partial_frame_split_across_two_feeds() -> None:
    parser = RadioStatusParser()
    frame = _radio_status_frame(rssi=123, remrssi=45)
    cut = len(frame) // 2

    first = parser.feed(frame[:cut])
    second = parser.feed(frame[cut:])

    assert first == []
    assert len(second) == 1
    assert second[0].rssi == 123
    assert second[0].remrssi == 45


def test_garbage_prefix_is_ignored() -> None:
    parser = RadioStatusParser()
    frame = _radio_status_frame(rssi=11, remrssi=22)
    junk = b"\x00\x01\x02hello, this isn't mavlink "

    result = parser.feed(junk + frame)

    assert len(result) == 1
    assert result[0].rssi == 11
    assert result[0].remrssi == 22


def test_two_back_to_back_frames() -> None:
    parser = RadioStatusParser()
    mav = _make_mav()
    a = _radio_status_frame(rssi=10, remrssi=20, mav=mav)
    b = _radio_status_frame(rssi=30, remrssi=40, mav=mav)

    result = parser.feed(a + b)

    assert len(result) == 2
    assert (result[0].rssi, result[0].remrssi) == (10, 20)
    assert (result[1].rssi, result[1].remrssi) == (30, 40)


def test_non_radio_status_messages_are_filtered_out() -> None:
    parser = RadioStatusParser()
    mav = _make_mav()
    hb = _heartbeat_frame(mav=mav)
    rs = _radio_status_frame(rssi=77, remrssi=88, mav=mav)

    result = parser.feed(hb + rs + hb)

    assert len(result) == 1
    assert result[0].rssi == 77
    assert result[0].remrssi == 88


def test_reset_discards_partial_frame() -> None:
    parser = RadioStatusParser()
    frame = _radio_status_frame(rssi=200, remrssi=190)

    half = parser.feed(frame[: len(frame) // 2])
    assert half == []

    parser.reset()

    fresh = _radio_status_frame(rssi=55, remrssi=66)
    result = parser.feed(fresh)

    assert len(result) == 1
    assert result[0].rssi == 55
    assert result[0].remrssi == 66


def test_bad_crc_is_dropped_silently() -> None:
    parser = RadioStatusParser()
    frame = bytearray(_radio_status_frame())
    frame[-1] ^= 0xFF

    result = parser.feed(bytes(frame))

    assert result == []


def test_empty_feed_returns_empty_list() -> None:
    parser = RadioStatusParser()
    assert parser.feed(b"") == []


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7])
def test_byte_by_byte_streaming(chunk_size: int) -> None:
    parser = RadioStatusParser()
    frame = _radio_status_frame(rssi=99, remrssi=88, txbuf=50)

    collected: list[RadioStatus] = []
    for i in range(0, len(frame), chunk_size):
        collected.extend(parser.feed(frame[i : i + chunk_size]))

    assert len(collected) == 1
    assert collected[0].rssi == 99
    assert collected[0].remrssi == 88
    assert collected[0].txbuf == 50
