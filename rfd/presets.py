"""Configuration presets and JSON profile import/export.

A *profile* is a JSON document like::

    {
      "format_version": 1,
      "name": "MAVLink defaults",
      "description": "...",
      "s_registers": {"1": 57, "2": 64, ...},
      "pin_registers": {}
    }

Built-in presets are defined in :data:`BUILT_IN_PRESETS` for the UI's
"Apply preset" menu.  They are conservative starting points — region and
frequency choices are deliberately omitted (the user must set MIN_FREQ /
MAX_FREQ explicitly to avoid stomping on regulatory limits).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from os import PathLike
from pathlib import Path

FORMAT_VERSION = 1


@dataclass
class Profile:
    name: str
    description: str = ""
    s_registers: dict[int, int] = field(default_factory=dict)
    pin_registers: dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "name": self.name,
            "description": self.description,
            "s_registers": {str(k): int(v) for k, v in self.s_registers.items()},
            "pin_registers": {str(k): int(v) for k, v in self.pin_registers.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        if int(d.get("format_version", 1)) != FORMAT_VERSION:
            raise ValueError(
                f"unsupported profile format_version {d.get('format_version')}"
            )
        s = {int(k): int(v) for k, v in (d.get("s_registers") or {}).items()}
        pin = {int(k): int(v) for k, v in (d.get("pin_registers") or {}).items()}
        return cls(
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            s_registers=s,
            pin_registers=pin,
        )


def save_profile(path: str | PathLike[str], profile: Profile) -> None:
    Path(path).write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")


def load_profile(path: str | PathLike[str]) -> Profile:
    return Profile.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------- built-ins
# Keys are S-register numbers.  Frequencies (S8/S9) and DUTY_CYCLE (S11) are
# left unset on purpose — those depend on the radio's region/band.
BUILT_IN_PRESETS: list[Profile] = [
    Profile(
        name="MAVLink defaults",
        description=(
            "Standard ArduPilot/PX4 telemetry setup: 57600 serial baud, "
            "64 kbps air rate, MAVLink framing on, ECC off."
        ),
        s_registers={
            1: 57,    # SERIAL_SPEED 57600
            2: 64,    # AIR_SPEED   64 kbps
            3: 25,    # NETID
            4: 20,    # TXPOWER     20 dBm
            5: 0,     # ECC off
            6: 1,     # MAVLINK framing
            7: 0,     # OPPRESEND off
            10: 20,   # NUM_CHANNELS
            13: 0,    # MANCHESTER off
            14: 0,    # RTSCTS off
            15: 131,  # MAX_WINDOW
        },
    ),
    Profile(
        name="Long range / low data rate",
        description=(
            "Maximum link margin: 8 kbps air rate, ECC on, max TX power. "
            "Use when range matters more than throughput."
        ),
        s_registers={
            1: 57,
            2: 8,     # AIR_SPEED 8 kbps
            3: 25,
            4: 30,    # 30 dBm = 1 W on RFD900x
            5: 1,     # ECC on
            6: 1,
            7: 1,     # OPPRESEND on
            10: 20,
            13: 0,
            14: 0,
            15: 131,
        },
    ),
    Profile(
        name="Point-to-point max throughput",
        description=(
            "Highest practical air rate, ECC off, MAVLink framing off. "
            "Best for raw serial bridging with strong link margin."
        ),
        s_registers={
            1: 115,   # SERIAL_SPEED 115200
            2: 250,   # AIR_SPEED 250 kbps
            3: 25,
            4: 20,
            5: 0,
            6: 0,     # raw passthrough
            7: 0,
            10: 20,
            13: 0,
            14: 1,    # RTSCTS on (recommended at high serial rates)
            15: 33,   # smaller window for lower latency
        },
    ),
]


def find_preset(name: str) -> Profile | None:
    for p in BUILT_IN_PRESETS:
        if p.name == name:
            return p
    return None
