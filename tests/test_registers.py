"""Tests for rfd.registers."""
from __future__ import annotations

import pytest

from rfd.registers import (
    PIN_REGISTERS,
    REGISTERS,
    RegisterDef,
    all_pin_registers,
    all_registers,
    get_register,
    validate,
)


# --------------------------------------------------------------------------- coverage

def test_all_s_registers_present() -> None:
    # S0..S15 are the canonical SiK set; S16..S29 are the "advanced" rows
    # added for newer firmware (RFD900x2 SiK 3.x). The full set must be
    # contiguous and start at 0.
    keys = set(REGISTERS.keys())
    assert {*range(16)} <= keys
    assert keys == set(range(min(keys), max(keys) + 1))


def test_all_pin_registers_present() -> None:
    assert set(PIN_REGISTERS.keys()) == set(range(16))


def test_all_registers_returns_sorted() -> None:
    regs = all_registers()
    sregs = [r.sreg for r in regs]
    assert sregs == sorted(sregs)
    assert sregs[0] == 0
    # Must include the canonical 0..15 range plus the advanced rows.
    assert set(range(16)) <= set(sregs)


def test_all_pin_registers_returns_sorted_16() -> None:
    regs = all_pin_registers()
    assert len(regs) == 16
    assert [r.sreg for r in regs] == list(range(16))


# --------------------------------------------------------------------------- structural invariants

@pytest.mark.parametrize("reg", list(REGISTERS.values()) + list(PIN_REGISTERS.values()))
def test_register_invariants(reg: RegisterDef) -> None:
    assert isinstance(reg.name, str) and reg.name
    assert isinstance(reg.label, str) and reg.label
    assert isinstance(reg.tooltip, str) and reg.tooltip
    assert reg.kind in {"int", "enum", "bool"}

    if reg.kind == "int":
        assert reg.enum is None
        if reg.minimum is not None and reg.maximum is not None:
            assert reg.minimum <= reg.maximum
    elif reg.kind == "enum":
        assert isinstance(reg.enum, dict) and reg.enum
        assert reg.minimum is None and reg.maximum is None
        for k, v in reg.enum.items():
            assert isinstance(k, int)
            assert isinstance(v, str) and v
    elif reg.kind == "bool":
        # Encoded as a 0/1 enum so the UI can render labels.
        assert isinstance(reg.enum, dict)
        assert set(reg.enum.keys()) == {0, 1}
        assert reg.minimum is None and reg.maximum is None


def test_register_def_is_frozen() -> None:
    reg = REGISTERS[3]
    with pytest.raises(Exception):
        reg.name = "NOPE"  # type: ignore[misc]


# --------------------------------------------------------------------------- specific register content

def test_s0_format_is_read_only() -> None:
    s0 = REGISTERS[0]
    assert s0.name == "FORMAT"
    assert s0.read_only is True
    assert s0.default is None


def test_s1_serial_speed_keys() -> None:
    s1 = REGISTERS[1]
    assert s1.name == "SERIAL_SPEED"
    assert s1.kind == "enum"
    assert set(s1.enum or {}) == {1, 2, 4, 9, 19, 38, 57, 115, 230}
    assert s1.default == 57
    assert s1.units == "kbps"


def test_s2_air_speed_is_superset() -> None:
    s2 = REGISTERS[2]
    assert s2.name == "AIR_SPEED"
    assert s2.kind == "enum"
    expected = {2, 4, 8, 16, 19, 24, 32, 48, 64, 96, 128, 192, 200, 224, 250}
    assert set(s2.enum or {}) == expected
    assert s2.default == 64
    assert s2.units == "kbps"
    assert "RFD900x" in s2.variant_notes


def test_s3_netid_range() -> None:
    s3 = REGISTERS[3]
    assert s3.kind == "int"
    assert s3.minimum == 0 and s3.maximum == 499
    assert s3.default == 25


def test_s4_txpower() -> None:
    s4 = REGISTERS[4]
    assert s4.minimum == 0 and s4.maximum == 30
    assert s4.default == 20
    assert s4.units == "dBm"
    assert "RFD900x" in s4.variant_notes


def test_s5_ecc_bool() -> None:
    s5 = REGISTERS[5]
    assert s5.kind == "bool"
    assert s5.default == 0
    assert s5.enum == {0: "Off", 1: "On"}


def test_s6_mavlink_enum() -> None:
    s6 = REGISTERS[6]
    assert s6.kind == "enum"
    assert s6.default == 1
    assert (s6.enum or {})[2] == "Low-latency MAVLink"


def test_s7_oppresend_tooltip_mentions_opportunistic_resend() -> None:
    s7 = REGISTERS[7]
    assert "opportunistic" in s7.tooltip.lower()


def test_s8_s9_freq_range_and_no_default() -> None:
    s8, s9 = REGISTERS[8], REGISTERS[9]
    for r in (s8, s9):
        assert r.minimum == 414000 and r.maximum == 976000
        assert r.units == "kHz"
        assert r.default is None
    assert "MIN_FREQ" in s9.tooltip


def test_s10_num_channels() -> None:
    s10 = REGISTERS[10]
    assert s10.minimum == 1 and s10.maximum == 50
    assert s10.default == 20


def test_s11_duty_cycle() -> None:
    s11 = REGISTERS[11]
    assert s11.minimum == 10 and s11.maximum == 100
    assert s11.default == 100
    assert s11.units == "%"


def test_s12_lbt_rssi_tooltip_mentions_lbt() -> None:
    s12 = REGISTERS[12]
    assert s12.minimum == 0 and s12.maximum == 220
    assert s12.default == 0
    assert "Listen-Before-Talk" in s12.tooltip


def test_s13_manchester() -> None:
    s13 = REGISTERS[13]
    assert s13.kind == "enum"
    assert s13.default == 0


def test_s14_rtscts() -> None:
    s14 = REGISTERS[14]
    assert s14.kind == "enum"
    assert s14.default == 0


def test_s15_max_window() -> None:
    s15 = REGISTERS[15]
    # Newer firmware reports 0 here; we widened the lower bound to 0.
    assert s15.minimum == 0 and s15.maximum == 131
    assert s15.default == 131
    assert s15.units == "ms"


def test_s16_through_s29_are_advanced_rows() -> None:
    for sreg in range(16, 30):
        r = REGISTERS[sreg]
        assert r.kind == "int"
        assert r.minimum == 0
        assert r.maximum == 65535
        assert r.label.startswith(f"S{sreg}")
        assert "advanced" in r.label.lower() or "advanced" in r.tooltip.lower()


# --------------------------------------------------------------------------- defaults validate

def test_all_defaults_validate() -> None:
    for sreg, reg in REGISTERS.items():
        if reg.default is None:
            continue
        ok, reason = validate(sreg, reg.default)
        assert ok, f"S{sreg} {reg.name} default {reg.default} failed: {reason}"
        assert reason == ""


def test_all_pin_defaults_validate() -> None:
    for sreg, reg in PIN_REGISTERS.items():
        if reg.default is None:
            continue
        ok, reason = validate(sreg, reg.default, pin=True)
        assert ok, f"R{sreg} {reg.name} default {reg.default} failed: {reason}"


# --------------------------------------------------------------------------- validate(): int

def test_int_in_range_ok() -> None:
    ok, reason = validate(3, 25)
    assert ok and reason == ""

    ok, _ = validate(3, 0)
    assert ok
    ok, _ = validate(3, 499)
    assert ok


def test_int_below_min_rejected() -> None:
    ok, reason = validate(3, -1)
    assert not ok
    assert "S3" in reason and "NETID" in reason
    assert "0..499" in reason


def test_int_above_max_rejected() -> None:
    ok, reason = validate(3, 700)
    assert not ok
    assert "700" in reason
    assert "0..499" in reason


def test_txpower_boundaries() -> None:
    assert validate(4, 0)[0]
    assert validate(4, 30)[0]
    assert not validate(4, 31)[0]
    assert not validate(4, -1)[0]


def test_max_window_boundaries() -> None:
    # S15 was widened to min=0 to accommodate newer firmware that reports 0.
    assert validate(15, 0)[0]
    assert validate(15, 33)[0]
    assert validate(15, 131)[0]
    assert not validate(15, 132)[0]
    assert not validate(15, -1)[0]


# --------------------------------------------------------------------------- validate(): enum

def test_enum_known_value_ok() -> None:
    ok, reason = validate(1, 57)
    assert ok and reason == ""


def test_enum_unknown_value_rejected() -> None:
    ok, reason = validate(1, 7)
    assert not ok
    assert "S1" in reason and "SERIAL_SPEED" in reason


def test_air_speed_supported_value_ok() -> None:
    for v in (2, 64, 250):
        ok, _ = validate(2, v)
        assert ok, v


def test_air_speed_unsupported_value_rejected() -> None:
    ok, reason = validate(2, 999)
    assert not ok
    assert "AIR_SPEED" in reason
    assert "999" in reason
    assert "not a supported air rate" in reason


def test_mavlink_enum_values() -> None:
    for v in (0, 1, 2):
        assert validate(6, v)[0]
    assert not validate(6, 3)[0]


# --------------------------------------------------------------------------- validate(): bool

def test_bool_accepts_zero_and_one() -> None:
    assert validate(5, 0)[0]
    assert validate(5, 1)[0]


def test_bool_rejects_other() -> None:
    ok, reason = validate(5, 2)
    assert not ok
    assert "ECC" in reason


# --------------------------------------------------------------------------- validate(): read-only and unknown

def test_read_only_register_rejects_write() -> None:
    ok, reason = validate(0, 1)
    assert not ok
    assert "FORMAT" in reason
    assert "read-only" in reason


def test_unknown_sreg_rejected() -> None:
    ok, reason = validate(99, 0)
    assert not ok
    assert "unknown register" in reason
    assert "S99" in reason


def test_unknown_pin_register_rejected() -> None:
    ok, reason = validate(99, 0, pin=True)
    assert not ok
    assert "unknown register" in reason
    assert "R99" in reason


# --------------------------------------------------------------------------- pin register validation

def test_pin_validate_accepts_known_function() -> None:
    for v in range(5):
        ok, reason = validate(0, v, pin=True)
        assert ok, (v, reason)


def test_pin_validate_rejects_unknown_function() -> None:
    ok, reason = validate(0, 9, pin=True)
    assert not ok
    assert "R0" in reason
    assert "PIN_R0" in reason


def test_pin_register_does_not_collide_with_s_register() -> None:
    # Same numeric sreg, different table — pin=True should consult PIN_REGISTERS
    # only. S0 is read-only; R0 is an enum that accepts 0..4.
    ok_pin, _ = validate(0, 1, pin=True)
    assert ok_pin
    ok_s, reason_s = validate(0, 1, pin=False)
    assert not ok_s
    assert "read-only" in reason_s


# --------------------------------------------------------------------------- accessors

def test_get_register_returns_correct_entry() -> None:
    assert get_register(3).name == "NETID"
    assert get_register(0, pin=True).name == "PIN_R0"


def test_get_register_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get_register(99)
    with pytest.raises(KeyError):
        get_register(99, pin=True)
