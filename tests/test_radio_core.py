"""Tests for the synchronous RadioCore protocol driver.  No Qt."""
from __future__ import annotations

import time

import pytest

from rfd.protocol import CommandModeBracket
from rfd.radio import RadioCore, RadioError


# Speed up the bracket so tests don't take 2+ seconds each.
FAST_BRACKET = CommandModeBracket(quiet_before=0.01, quiet_after=0.01, reply_timeout=0.5)


def test_enter_command_mode_ok(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"AT\r\n", b"OK\r\n")
    assert core.enter_command_mode(FAST_BRACKET) is True
    assert b"+++" in mock_serial.written
    assert b"AT\r\n" in mock_serial.written


def test_enter_command_mode_fail(mock_serial):
    core = RadioCore(mock_serial)
    # No script -> AT gets no reply.
    mock_serial.timeout = 0.05
    assert core.enter_command_mode(FAST_BRACKET) is False


def test_exit_command_mode(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"ATO\r\n", b"OK\r\n")
    assert core.exit_command_mode() is True
    assert b"ATO\r\n" in mock_serial.written


ATI5_REPLY = (
    "ATI5\r\n"
    "S0: FORMAT=25\r\n"
    "S1: SERIAL_SPEED=57\r\n"
    "S2: AIR_SPEED=64\r\n"
    "S3: NETID=25\r\n"
    "S4: TXPOWER=20\r\n"
    "S5: ECC=0\r\n"
    "S6: MAVLINK=1\r\n"
    "S7: OPPRESEND=0\r\n"
    "S8: MIN_FREQ=915000\r\n"
    "S9: MAX_FREQ=928000\r\n"
    "S10: NUM_CHANNELS=20\r\n"
    "S11: DUTY_CYCLE=100\r\n"
    "S12: LBT_RSSI=0\r\n"
    "S13: MANCHESTER=0\r\n"
    "S14: RTSCTS=0\r\n"
    "S15: MAX_WINDOW=131\r\n"
    "OK\r\n"
)


def test_read_local_params(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"ATI5\r\n", ATI5_REPLY.encode("ascii"))
    res = core.read_params()
    assert len(res.s_params) == 16
    assert res.s_params[3] == 25
    assert res.s_params[8] == 915000


def test_read_remote_params(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"RTI5\r\n", ATI5_REPLY.replace("ATI5", "RTI5").encode("ascii"))
    res = core.read_params(remote=True)
    assert res.s_params[3] == 25


def test_read_params_empty_raises(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"ATI5\r\n", b"OK\r\n")
    with pytest.raises(RadioError):
        core.read_params(timeout=0.2)


def test_write_param(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"ATS3=42\r\n", b"OK\r\n")
    assert core.write_param(3, 42) is True
    assert b"ATS3=42\r\n" in mock_serial.written


def test_write_param_remote_pin(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"RTR5=2\r\n", b"OK\r\n")
    assert core.write_param(5, 2, remote=True, pin=True) is True
    assert b"RTR5=2\r\n" in mock_serial.written


def test_write_param_failure(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.timeout = 0.05
    mock_serial.script(b"ATS3=999\r\n", b"ERROR\r\n")
    assert core.write_param(3, 999) is False


def test_save_eeprom(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"AT&W\r\n", b"OK\r\n")
    assert core.save_eeprom() is True


def test_save_eeprom_remote(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"RT&W\r\n", b"OK\r\n")
    assert core.save_eeprom(remote=True) is True


def test_factory_reset(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"AT&F\r\n", b"OK\r\n")
    assert core.factory_reset() is True


def test_reboot_no_reply_expected(mock_serial):
    core = RadioCore(mock_serial)
    core.reboot()
    assert b"ATZ\r\n" in mock_serial.written


def test_send_at_appends_crlf(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"ATI\r\n", b"SiK 2.0 on RFD900x\r\nOK\r\n")
    reply = core.send_at("ATI")
    assert "RFD900x" in reply
    assert b"ATI\r\n" in mock_serial.written


def test_poll_rssi(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(
        b"ATI7\r\n",
        b"L/R RSSI: 222/216  L/R noise: 30/29 pkts: 1234 txe=0 rxe=0 stx=0 srx=0 ecc=0/0 temp=42\r\nOK\r\n",
    )
    r = core.poll_rssi()
    assert r.local_rssi == 222
    assert r.remote_rssi == 216
    assert r.temp_c == 42


def test_identify(mock_serial):
    core = RadioCore(mock_serial)
    mock_serial.script(b"ATI\r\n", b"SiK 2.0 on RFD900x\r\nOK\r\n")
    mock_serial.script(b"ATI2\r\n", b"123\r\nOK\r\n")
    mock_serial.script(b"ATI3\r\n", b"915\r\nOK\r\n")
    mock_serial.script(b"ATI4\r\n", b"3.0.0\r\nOK\r\n")
    info = core.identify(timeout=0.2)
    assert info["banner"] == "SiK 2.0 on RFD900x"
    assert info["board_id"] == 123
    assert info["board_name"] == "RFD900x"   # 0x7B = 123
    assert info["freq_id"] == 915
    assert info["bootloader_version"] == "3.0.0"


def test_enter_bootloader(mock_serial):
    core = RadioCore(mock_serial)
    core.enter_bootloader()
    assert b"AT&UPDATE\r\n" in mock_serial.written


def test_send_collect_idle_gap_returns_early(mock_serial):
    core = RadioCore(mock_serial)
    # Pre-feed a small reply; idle_gap should let _send_collect return well
    # before the full timeout.
    mock_serial.script(b"ATI\r\n", b"banner\r\n")
    t0 = time.monotonic()
    reply = core._send_collect(b"ATI\r\n", timeout=2.0, idle_gap=0.05)
    elapsed = time.monotonic() - t0
    assert "banner" in reply
    assert elapsed < 1.0
