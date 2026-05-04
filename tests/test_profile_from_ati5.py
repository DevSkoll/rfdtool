"""Tests for `presets.profile_from_ati5` — the live-capture helper that
turns a fresh ATI5 read into a transferable Profile."""
from __future__ import annotations

import pytest

from rfd.presets import profile_from_ati5, save_profile, load_profile, Profile
from rfd.protocol import Ati5Result


def _make_ati5(s_params: dict[int, int],
               s_names: dict[int, str],
               pin_params: dict[int, int] | None = None,
               pin_names: dict[int, str] | None = None) -> Ati5Result:
    return Ati5Result(
        s_params=dict(s_params),
        pin_params=dict(pin_params or {}),
        s_names=dict(s_names),
        pin_names=dict(pin_names or {}),
    )


def test_basic_capture_uses_firmware_names():
    ati5 = _make_ati5(
        s_params={0: 63, 1: 57, 2: 64, 3: 41},
        s_names={0: "FORMAT", 1: "SERIAL_SPEED", 2: "AIR_SPEED", 3: "NETID"},
    )
    p = profile_from_ati5("snap", ati5)
    # FORMAT is read-only and must be skipped
    assert "FORMAT" not in p.params
    assert p.params["SERIAL_SPEED"] == 57
    assert p.params["AIR_SPEED"] == 64
    assert p.params["NETID"] == 41


def test_preserves_unknown_firmware_names():
    """RFDesign 3.x has ENCRYPTION_LEVEL, AIR_FRAMELEN etc. that aren't in
    the canonical SiK enum — they should round-trip via the catalog's
    'unknown name = accept' branch."""
    ati5 = _make_ati5(
        s_params={2: 64, 14: 131, 15: 0, 26: 120, 28: 50},
        s_names={
            2: "AIR_SPEED",
            14: "MAX_WINDOW",
            15: "ENCRYPTION_LEVEL",   # not on canonical SiK
            26: "AIR_FRAMELEN",
            28: "FSFRAMELOSS",
        },
    )
    p = profile_from_ati5("rfdesign", ati5)
    assert p.params["AIR_SPEED"] == 64
    assert p.params["MAX_WINDOW"] == 131
    assert p.params["ENCRYPTION_LEVEL"] == 0
    assert p.params["AIR_FRAMELEN"] == 120
    assert p.params["FSFRAMELOSS"] == 50


def test_radio_info_embeds_metadata_in_notes():
    ati5 = _make_ati5(s_params={3: 41}, s_names={3: "NETID"})
    info = {
        "banner": "RFD SiK 3.57 on RFD900X2-US",
        "board_name": "RFD900x2",
        "board_id": 132,
        "freq_id": None,
        "bootloader_version": "BoardRev: 10",
    }
    p = profile_from_ati5("snap", ati5, radio_info=info, notes="from bench unit")
    assert "Captured from:" in p.notes
    assert "RFD900x2" in p.notes
    assert "RFD SiK 3.57 on RFD900X2-US" in p.notes
    assert "from bench unit" in p.notes
    # None / empty fields are skipped
    assert "freq_id" not in p.notes


def test_applies_to_stamped_from_board_name():
    ati5 = _make_ati5(s_params={3: 41}, s_names={3: "NETID"})
    p = profile_from_ati5(
        "snap", ati5,
        radio_info={"board_name": "RFD900x2"},
    )
    assert p.applies_to == ["RFD900x2"]


def test_applies_to_skipped_for_unknown_board():
    ati5 = _make_ati5(s_params={3: 41}, s_names={3: "NETID"})
    # protocol.board_name() returns "Unknown (ID 0xNN)" when ID isn't in the table.
    p = profile_from_ati5(
        "snap", ati5,
        radio_info={"board_name": "Unknown (ID 0x99)"},
    )
    assert p.applies_to == []


def test_pin_params_round_trip():
    ati5 = _make_ati5(
        s_params={3: 41}, s_names={3: "NETID"},
        pin_params={0: 0, 1: 5},
        pin_names={0: "TARGET_RSSI_dBm", 1: "HYSTERESIS_RSSI_dBm"},
    )
    p = profile_from_ati5("snap", ati5)
    assert p.pin_registers == {0: 0, 1: 5}


def test_json_round_trip(tmp_path):
    ati5 = _make_ati5(
        s_params={1: 57, 2: 64, 3: 41, 14: 131, 15: 0, 26: 120},
        s_names={
            1: "SERIAL_SPEED", 2: "AIR_SPEED", 3: "NETID",
            14: "MAX_WINDOW", 15: "ENCRYPTION_LEVEL", 26: "AIR_FRAMELEN",
        },
    )
    info = {"banner": "test banner", "board_name": "RFD900x2"}
    p = profile_from_ati5("round-trip", ati5, radio_info=info)

    path = tmp_path / "snap.json"
    save_profile(path, p)
    p2 = load_profile(path)

    assert p2.name == p.name
    assert p2.params == p.params
    assert p2.applies_to == ["RFD900x2"]
    assert "test banner" in p2.notes


def test_empty_ati5_yields_empty_profile():
    ati5 = _make_ati5(s_params={}, s_names={})
    p = profile_from_ati5("empty", ati5)
    assert p.params == {}
    assert p.pin_registers == {}


def test_skips_sregs_without_a_reported_name():
    """If the parser couldn't identify a register's name (rare; happens with
    weird firmware output), skip rather than guess via canonical mapping."""
    ati5 = _make_ati5(
        s_params={3: 41, 99: 7},   # S99 has no reported name
        s_names={3: "NETID"},
    )
    p = profile_from_ati5("snap", ati5)
    assert p.params == {"NETID": 41}


def test_can_construct_without_radio_info():
    ati5 = _make_ati5(s_params={3: 41}, s_names={3: "NETID"})
    p = profile_from_ati5("no-info", ati5)
    assert p.applies_to == []
    assert p.notes == ""
    assert p.params == {"NETID": 41}
