from __future__ import annotations

import pytest

from rfd import protocol


# -------------------------------------------------- AT command builders
def test_at_set_param_local_and_remote():
    assert protocol.at_set_param(3, 25) == b"ATS3=25\r\n"
    assert protocol.at_set_param(3, 25, remote=True) == b"RTS3=25\r\n"


def test_at_set_pin():
    assert protocol.at_set_pin(0, 1) == b"ATR0=1\r\n"
    assert protocol.at_set_pin(15, 4, remote=True) == b"RTR15=4\r\n"


@pytest.mark.parametrize("fn,local,remote", [
    (protocol.at_save_eeprom, b"AT&W\r\n", b"RT&W\r\n"),
    (protocol.at_reboot, b"ATZ\r\n", b"RTZ\r\n"),
    (protocol.at_factory_reset, b"AT&F\r\n", b"RT&F\r\n"),
    (protocol.at_read_params, b"ATI5\r\n", b"RTI5\r\n"),
])
def test_at_simple_commands(fn, local, remote):
    assert fn() == local
    assert fn(remote=True) == remote


def test_at_misc_commands():
    assert protocol.at_rssi() == b"ATI7\r\n"
    assert protocol.at_bootloader() == b"AT&UPDATE\r\n"
    assert protocol.at_exit_command_mode() == b"ATO\r\n"
    assert protocol.at_identify() == b"ATI\r\n"
    assert protocol.at_board_id() == b"ATI2\r\n"
    assert protocol.at_freq_id() == b"ATI3\r\n"
    assert protocol.at_bootloader_version() == b"ATI4\r\n"


# -------------------------------------------------- ATI5 parser
ATI5_SAMPLE = """ATI5
S0: FORMAT=25
S1: SERIAL_SPEED=57
S2: AIR_SPEED=64
S3: NETID=25
S4: TXPOWER=20
S5: ECC=0
S6: MAVLINK=1
S7: OPPRESEND=0
S8: MIN_FREQ=915000
S9: MAX_FREQ=928000
S10: NUM_CHANNELS=20
S11: DUTY_CYCLE=100
S12: LBT_RSSI=0
S13: MANCHESTER=0
S14: RTSCTS=0
S15: MAX_WINDOW=131
"""


def test_parse_ati5_full():
    res = protocol.parse_ati5(ATI5_SAMPLE)
    assert len(res.s_params) == 16
    assert res.s_params[0] == 25
    assert res.s_params[2] == 64
    assert res.s_params[8] == 915000
    assert res.s_params[15] == 131
    assert res.pin_params == {}


def test_parse_ati5_with_pin_registers():
    text = ATI5_SAMPLE + "R0: PIN_FUNC=0\nR15: PIN_FUNC=4\n"
    res = protocol.parse_ati5(text)
    assert len(res.s_params) == 16
    assert res.pin_params == {0: 0, 15: 4}


def test_parse_ati5_crlf_and_noise():
    text = "ATI5\r\nS1: SERIAL_SPEED=57\r\n\r\n  garbage line  \r\nS2: AIR_SPEED=64\r\nOK\r\n"
    res = protocol.parse_ati5(text)
    assert res.s_params == {1: 57, 2: 64}


def test_parse_ati5_empty():
    assert protocol.parse_ati5("").s_params == {}


# -------------------------------------------------- ATI7 parser
def test_parse_ati7_full_line():
    text = ("L/R RSSI: 222/216  L/R noise: 30/29 pkts: 12345 "
            " txe=0 rxe=2 stx=0 srx=1 ecc=3/1 temp=42")
    r = protocol.parse_ati7(text)
    assert r.local_rssi == 222
    assert r.remote_rssi == 216
    assert r.local_noise == 30
    assert r.remote_noise == 29
    assert r.pkts == 12345
    assert r.txe == 0 and r.rxe == 2 and r.stx == 0 and r.srx == 1
    assert r.ecc_corrected == 3 and r.ecc_uncorrected == 1
    assert r.temp_c == 42


def test_parse_ati7_partial():
    text = "L/R RSSI: 100/95"
    r = protocol.parse_ati7(text)
    assert r.local_rssi == 100 and r.remote_rssi == 95
    assert r.local_noise is None and r.pkts is None


def test_parse_ati7_negative_temp():
    text = "L/R RSSI: 1/2 temp=-273"
    r = protocol.parse_ati7(text)
    assert r.temp_c == -273


def test_parse_ati7_keeps_raw():
    text = "  L/R RSSI: 1/2 \n"
    r = protocol.parse_ati7(text)
    assert "L/R RSSI" in r.raw


# -------------------------------------------------- single-line parsers
def test_parse_banner_skips_echo_and_ok():
    assert protocol.parse_banner("ATI\r\nSiK 2.0 on RFD900x\r\nOK\r\n") == "SiK 2.0 on RFD900x"


def test_parse_banner_falls_back_to_first_nonempty():
    assert protocol.parse_banner("just a thing") == "just a thing"


def test_parse_int_response_decimal():
    assert protocol.parse_int_response("ATI2\r\n123\r\nOK\r\n") == 123


def test_parse_int_response_hex():
    assert protocol.parse_int_response("ATI2\r\n0x7B\r\n") == 0x7B


def test_parse_int_response_negative():
    assert protocol.parse_int_response("-5") == -5


def test_parse_int_response_none():
    assert protocol.parse_int_response("OK\r\n") is None


# -------------------------------------------------- board id helpers
def test_board_name_known():
    assert protocol.board_name(0x7B) == "RFD900x"
    assert protocol.board_name(0x50) == "RFD900p"


def test_board_name_unknown_keeps_id():
    assert "0x99" in protocol.board_name(0x99)


def test_board_name_none():
    assert protocol.board_name(None) == "Unknown"


def test_is_stm32_board():
    assert protocol.is_stm32_board(0x7B) is True   # RFD900x
    assert protocol.is_stm32_board(0x7D) is True   # RFD900ux
    assert protocol.is_stm32_board(0x50) is False  # RFD900+
    assert protocol.is_stm32_board(None) is False
