from __future__ import annotations

import json

import pytest

from rfd import presets
from rfd.presets import BUILT_IN_PRESETS, Profile, load_profile, save_profile


def test_built_ins_have_unique_names():
    names = [p.name for p in BUILT_IN_PRESETS]
    assert len(names) == len(set(names))
    assert {"MAVLink defaults", "Long range / low data rate",
            "Point-to-point max throughput"} <= set(names)


def test_round_trip_via_dict():
    p = Profile(
        name="test",
        description="desc",
        s_registers={1: 57, 2: 64},
        pin_registers={0: 1},
    )
    d = p.to_dict()
    assert d["format_version"] == presets.FORMAT_VERSION
    p2 = Profile.from_dict(d)
    assert p2.name == p.name
    assert p2.s_registers == p.s_registers
    assert p2.pin_registers == p.pin_registers


def test_dict_keys_become_strings_in_json():
    p = Profile(name="x", s_registers={1: 57})
    d = p.to_dict()
    # JSON serialisable
    s = json.dumps(d)
    assert '"1": 57' in s


def test_save_and_load_roundtrip(tmp_path):
    src = Profile(
        name="round-trip",
        description="hello",
        s_registers={3: 25, 4: 20},
        pin_registers={5: 2},
    )
    p = tmp_path / "profile.json"
    save_profile(p, src)
    out = load_profile(p)
    assert out.name == src.name
    assert out.s_registers == src.s_registers
    assert out.pin_registers == src.pin_registers


def test_unsupported_format_version_rejected():
    with pytest.raises(ValueError, match="format_version"):
        Profile.from_dict({"format_version": 99, "name": "x"})


def test_find_preset():
    p = presets.find_preset("MAVLink defaults")
    assert p is not None
    assert p.s_registers[2] == 64   # 64 kbps air rate
    assert presets.find_preset("nonexistent") is None


def test_preset_values_pass_register_validation():
    """Built-in presets must produce values that pass our own validator."""
    from rfd.registers import validate

    for preset in BUILT_IN_PRESETS:
        for sreg, value in preset.s_registers.items():
            ok, reason = validate(sreg, value)
            assert ok, f"preset '{preset.name}' S{sreg}={value}: {reason}"
        for pin, value in preset.pin_registers.items():
            ok, reason = validate(pin, value, pin=True)
            assert ok, f"preset '{preset.name}' R{pin}={value}: {reason}"
