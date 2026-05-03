"""Configuration presets and JSON profile import/export.

Two layers:

* **Profile** — a serialisable bundle of S-register and pin-register values
  with optional metadata (category, applies_to, notes).  JSON format v2 is
  backwards-compatible with v1 files (missing fields take defaults on load).
* **BUILT_IN_PRESETS** — the curated library shipped in code, derived from
  the SiK firmware defaults, the ArduPilot wiki, the RFDesign datasheets
  and the regional regulatory data in :mod:`rfd.regions`.  Four categories:

  ``region``    — sets only S8/S9/S10/S11/S12.  Applied on top of any other
                  preset to retune for a different country / band.
  ``use-case``  — sets only S2/S4/S5/S6/S7/S13/S14/S15.  Defines how the
                  link behaves (range vs. throughput, MAVLink framing, …).
  ``model``     — full SiK factory baseline for a given board family.
                  Skips S8/S9/S10 because those depend on region.
  ``maximize``  — all-in-one combinations of region + use-case + model TX
                  ceiling.  One-click "best for X on Y in Z" presets.

User-saved presets live as one JSON file per preset under
``~/.config/rfdtool/profiles/``.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path

FORMAT_VERSION = 2

# Categories the UI uses to group presets.
CATEGORY_REGION = "region"
CATEGORY_USE_CASE = "use-case"
CATEGORY_MODEL = "model"
CATEGORY_MAXIMIZE = "maximize"
CATEGORY_USER = "user"

ALL_CATEGORIES = (
    CATEGORY_REGION,
    CATEGORY_USE_CASE,
    CATEGORY_MODEL,
    CATEGORY_MAXIMIZE,
    CATEGORY_USER,
)


@dataclass
class Profile:
    name: str
    description: str = ""
    s_registers: dict[int, int] = field(default_factory=dict)
    pin_registers: dict[int, int] = field(default_factory=dict)
    # v2 metadata — all default-empty so v1 files load cleanly.
    applies_to: list[str] = field(default_factory=list)
    category: str = CATEGORY_USER
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "applies_to": list(self.applies_to),
            "notes": self.notes,
            "s_registers": {str(k): int(v) for k, v in self.s_registers.items()},
            "pin_registers": {str(k): int(v) for k, v in self.pin_registers.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        version = int(d.get("format_version", 1))
        if version not in (1, 2):
            raise ValueError(f"unsupported profile format_version {version}")
        s = {int(k): int(v) for k, v in (d.get("s_registers") or {}).items()}
        pin = {int(k): int(v) for k, v in (d.get("pin_registers") or {}).items()}
        return cls(
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            s_registers=s,
            pin_registers=pin,
            applies_to=list(d.get("applies_to") or []),
            category=str(d.get("category") or CATEGORY_USER),
            notes=str(d.get("notes") or ""),
        )

    def matches_board(self, board_name: str) -> bool:
        """True if this preset is applicable to `board_name`.

        applies_to=[] means "any board".  Match is case-insensitive
        substring so banner-style names like 'RFD900X2-US' resolve.
        """
        if not self.applies_to:
            return True
        if not board_name:
            return True
        lowered = board_name.lower()
        return any(b.lower() in lowered for b in self.applies_to)


# --------------------------------------------------------------------- JSON I/O
def save_profile(path: str | PathLike[str], profile: Profile) -> None:
    Path(path).write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")


def load_profile(path: str | PathLike[str]) -> Profile:
    return Profile.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------- user storage
def user_profile_dir() -> Path:
    """Resolve XDG_CONFIG_HOME/rfdtool/profiles, creating it on first call."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    p = Path(base) / "rfdtool" / "profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Filename-safe slug for a preset name."""
    s = _SLUG_RE.sub("-", name.lower()).strip("-")
    return s or "preset"


def list_user_profiles() -> list[Profile]:
    """Read every *.json file in user_profile_dir() into Profile objects.

    Files that fail to parse are skipped silently (a corrupt profile shouldn't
    block startup) — callers that want to surface errors can iterate manually.
    """
    out: list[Profile] = []
    for p in sorted(user_profile_dir().glob("*.json")):
        try:
            out.append(load_profile(p))
        except Exception:
            continue
    return out


def save_user_profile(profile: Profile, *, overwrite: bool = True) -> Path:
    """Write `profile` under user_profile_dir() with a slugified filename."""
    profile.category = profile.category or CATEGORY_USER
    target = user_profile_dir() / f"{slugify(profile.name)}.json"
    if target.exists() and not overwrite:
        raise FileExistsError(target)
    save_profile(target, profile)
    return target


def delete_user_profile(profile: Profile) -> bool:
    """Remove the on-disk file for `profile`. Returns True if removed."""
    target = user_profile_dir() / f"{slugify(profile.name)}.json"
    if target.exists():
        target.unlink()
        return True
    return False


# --------------------------------------------------------------------- built-ins
# All values cross-checked against:
#   - SiK parameters.c (default values)
#   - ArduPilot wiki RFD900 + Telemetry Radio Regional Regulations pages
#   - RFDesign datasheets / RFD900x peer-to-peer V3 manual
# Citations are inline in Profile.notes for region presets.

# ------------- A. Region & frequency (sets only S8/S9/S10/S11/S12)
REGION_PRESETS: list[Profile] = [
    Profile(
        name="US / Canada — 902-928 MHz",
        description="FCC 15.247 / IC RSS-210 — full ISM band, 50 channels.",
        category=CATEGORY_REGION,
        s_registers={8: 902000, 9: 928000, 10: 50, 11: 100, 12: 0},
        notes="FCC 15.247 (US) / IC RSS-210 Annex 8.1 (Canada). License-free.",
    ),
    Profile(
        name="Australia — 915-928 MHz",
        description="ACMA LIPD-2015 — narrower band, ≤1 W EIRP.",
        category=CATEGORY_REGION,
        s_registers={8: 915001, 9: 928000, 10: 20, 11: 100, 12: 0},
        notes="ACMA LIPD-2015. ≤1 W EIRP including antenna gain.",
    ),
    Profile(
        name="New Zealand — 921-928 MHz",
        description="NZ General User Radio Licence — 7 MHz sub-band.",
        category=CATEGORY_REGION,
        s_registers={8: 921000, 9: 928000, 10: 20, 11: 100, 12: 0},
        notes="NZ Notice 2007 Schedule 1 (General User Radio Licence).",
    ),
    Profile(
        name="Brazil — 915-928 MHz",
        description="ANATEL Resolução 506/2008 — at least 26 channels.",
        category=CATEGORY_REGION,
        s_registers={8: 915000, 9: 928000, 10: 26, 11: 100, 12: 0},
        notes="ANATEL Resolução 506/2008.",
    ),
    Profile(
        name="EU / UK — 433 MHz ISM",
        description="ETSI EN 300 220 — 10% duty cycle, LBT required.",
        category=CATEGORY_REGION,
        applies_to=["RFD900u"],   # 433-MHz hardware variant only
        s_registers={8: 433050, 9: 434790, 10: 10, 11: 10, 12: 25},
        notes=(
            "ETSI EN 300 220 V3.1.1 / OFCOM IR2030/1/10. Mandatory 10% duty "
            "cycle and LBT (S12 ≥ 25). ≤7 dBm conducted."
        ),
    ),
    Profile(
        name="Australia — 433 MHz",
        description="ACMA LIPD — ≤25 mW EIRP (≈14 dBm).",
        category=CATEGORY_REGION,
        applies_to=["RFD900u"],
        s_registers={8: 433051, 9: 434790, 10: 10, 11: 100, 12: 0},
        notes="ACMA LIPD class licence. ≤25 mW EIRP.",
    ),
]


# ------------- B. Use case (sets only S2/S4/S5/S6/S7/S13/S14/S15)
USE_CASE_PRESETS: list[Profile] = [
    Profile(
        name="MAVLink default (SiK factory)",
        description="Stock SiK factory: 64 kbps air, 20 dBm, MAVLink framing on, ECC off.",
        category=CATEGORY_USE_CASE,
        s_registers={2: 64, 4: 20, 5: 0, 6: 1, 7: 0, 13: 0, 14: 0, 15: 131},
        notes=(
            "Matches SiK firmware defaults. ECC is intentionally OFF — current "
            "ArduPilot guidance: 'Using error correction is no longer "
            "recommended due to the range reduction.'"
        ),
    ),
    Profile(
        name="MAVLink + RC joystick (low-latency)",
        description="Low-latency MAVLink: prioritises RC_OVERRIDE, small TDM window.",
        category=CATEGORY_USE_CASE,
        s_registers={2: 64, 4: 20, 5: 0, 6: 2, 7: 0, 13: 0, 14: 0, 15: 33},
        notes=(
            "S6=2 enables low-latency MAVLink with RC_OVERRIDE priority. "
            "S15=33 ms minimises TDM window for joystick responsiveness."
        ),
    ),
    Profile(
        name="MAVLink high throughput",
        description="128 kbps air with MAVLink framing + RTSCTS for high telemetry rates.",
        category=CATEGORY_USE_CASE,
        s_registers={2: 128, 4: 20, 5: 0, 6: 2, 7: 0, 13: 0, 14: 1, 15: 131},
        notes="RTSCTS recommended at high air rates to prevent UART overrun.",
    ),
    Profile(
        name="Long range — 8 kbps (20 dBm)",
        description="8 kbps air rate, 20 dBm, MAVLink framing — extends range without external power.",
        category=CATEGORY_USE_CASE,
        s_registers={2: 8, 4: 20, 5: 0, 6: 1, 7: 0, 13: 0, 14: 0, 15: 131},
        notes=(
            "Lower air rate trades throughput for receiver sensitivity (≈9 dB "
            "improvement vs. 64 kbps). ECC kept off per ArduPilot guidance."
        ),
    ),
    Profile(
        name="Long range — 8 kbps + 1 W TX",
        description="8 kbps air rate at 30 dBm (1 W) — maximum range.",
        category=CATEGORY_USE_CASE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        s_registers={2: 8, 4: 30, 5: 0, 6: 1, 7: 0, 13: 0, 14: 0, 15: 131},
        notes=(
            "Requires external ≥2 A power supply; autopilot telemetry ports "
            "typically can't deliver this. Combine with a region preset."
        ),
    ),
    Profile(
        name="Max throughput — 250 kbps + RTSCTS",
        description="Highest practical air rate, MAVLink framing OFF.",
        category=CATEGORY_USE_CASE,
        s_registers={2: 250, 4: 20, 5: 0, 6: 0, 7: 0, 13: 0, 14: 1, 15: 131},
        notes="Best for raw serial bridging or non-MAVLink data with strong link margin.",
    ),
    Profile(
        name="Raw serial bridge",
        description="64 kbps, no MAVLink framing, no ECC — generic UART tunnel.",
        category=CATEGORY_USE_CASE,
        s_registers={2: 64, 4: 20, 5: 0, 6: 0, 7: 0, 13: 0, 14: 0, 15: 131},
        notes="Use when bridging arbitrary serial data (modbus, NMEA, …) instead of MAVLink.",
    ),
    Profile(
        name="Robust (Manchester encoding)",
        description="Manchester-encoded for noisy RF environments; 8 kbps.",
        category=CATEGORY_USE_CASE,
        s_registers={2: 8, 4: 20, 5: 0, 6: 1, 7: 0, 13: 1, 14: 0, 15: 131},
        notes=(
            "Manchester encoding doubles air-bandwidth requirements but improves "
            "robustness against DC drift and certain interference patterns."
        ),
    ),
]


# ------------- C. Model factory defaults (sets full SiK baseline minus region)
MODEL_PRESETS: list[Profile] = [
    Profile(
        name="RFD900 / RFD900+ / RFD900u — factory defaults",
        description="SiK firmware factory defaults for the 8051 family.",
        category=CATEGORY_MODEL,
        applies_to=["RFD900", "RFD900a", "RFD900p", "RFD900u"],
        s_registers={
            1: 57, 2: 64, 3: 25, 4: 20, 5: 0, 6: 1, 7: 0,
            11: 100, 13: 0, 14: 0, 15: 131,
        },
        notes=(
            "Apply a Region preset afterwards to set S8/S9/S10. "
            "S1=57 will reset the radio's UART to 57600 baud."
        ),
    ),
    Profile(
        name="RFD900x / RFD900ux — factory defaults",
        description="SiK firmware factory defaults for the STM32 family (SiK 2.x).",
        category=CATEGORY_MODEL,
        applies_to=["RFD900x", "RFD900ux"],
        s_registers={
            1: 57, 2: 64, 3: 25, 4: 20, 5: 0, 6: 1, 7: 0,
            11: 100, 13: 0, 14: 0, 15: 131,
        },
        notes="Apply a Region preset afterwards to set S8/S9/S10.",
    ),
    Profile(
        name="RFD900x2 — factory defaults",
        description="SiK 3.x factory defaults for the RFD900x2.",
        category=CATEGORY_MODEL,
        applies_to=["RFD900x2"],
        s_registers={
            1: 57, 2: 64, 3: 25, 4: 20, 5: 0, 6: 1, 7: 0,
            11: 100, 13: 0, 14: 0, 15: 131,
        },
        notes=(
            "Apply a Region preset afterwards to set S8/S9/S10. "
            "Newer firmware adds advanced registers (S16+) which are left "
            "untouched by this preset."
        ),
    ),
]


# ------------- D. "Maximize" all-in-one combos
MAXIMIZE_PRESETS: list[Profile] = [
    Profile(
        name="Maximize MAVLink reliability — US (RFD900x/x2)",
        description="64 kbps + low-latency MAVLink, US 902-928 MHz, 20 dBm.",
        category=CATEGORY_MAXIMIZE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        s_registers={
            2: 64, 4: 20, 5: 0, 6: 2, 7: 0,
            8: 902000, 9: 928000, 10: 50, 11: 100, 12: 0,
            13: 0, 14: 0, 15: 131,
        },
        notes="Best autopilot default in the US/Canada band. FCC 15.247 compliant.",
    ),
    Profile(
        name="Maximize range — US, 1 W (RFD900x/x2)",
        description="8 kbps + 30 dBm, US 902-928 MHz — needs external power.",
        category=CATEGORY_MAXIMIZE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        s_registers={
            2: 8, 4: 30, 5: 0, 6: 1, 7: 0,
            8: 902000, 9: 928000, 10: 50, 11: 100, 12: 0,
            13: 0, 14: 0, 15: 131,
        },
        notes=(
            "30 dBm peak draws ≈2 A — supply external power, do not run from "
            "an autopilot telemetry port. FCC 15.247 compliant."
        ),
    ),
    Profile(
        name="Maximize throughput — US (RFD900x/x2)",
        description="250 kbps + RTSCTS, US 902-928 MHz, 20 dBm.",
        category=CATEGORY_MAXIMIZE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        s_registers={
            2: 250, 4: 20, 5: 0, 6: 0, 7: 0,
            8: 902000, 9: 928000, 10: 50, 11: 100, 12: 0,
            13: 0, 14: 1, 15: 131,
        },
        notes="MAVLink framing off (S6=0); pair with raw serial workflows.",
    ),
    Profile(
        name="Maximize MAVLink reliability — AU (RFD900x/x2)",
        description="64 kbps + low-latency MAVLink, AU 915-928 MHz, 20 dBm.",
        category=CATEGORY_MAXIMIZE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        s_registers={
            2: 64, 4: 20, 5: 0, 6: 2, 7: 0,
            8: 915001, 9: 928000, 10: 20, 11: 100, 12: 0,
            13: 0, 14: 0, 15: 131,
        },
        notes="ACMA LIPD-2015 compliant.",
    ),
    Profile(
        name="Maximize range — AU, 1 W (RFD900x/x2)",
        description="8 kbps + 30 dBm, AU 915-928 MHz — needs external power.",
        category=CATEGORY_MAXIMIZE,
        applies_to=["RFD900x", "RFD900ux", "RFD900x2"],
        s_registers={
            2: 8, 4: 30, 5: 0, 6: 1, 7: 0,
            8: 915001, 9: 928000, 10: 20, 11: 100, 12: 0,
            13: 0, 14: 0, 15: 131,
        },
        notes="≤1 W EIRP per ACMA LIPD-2015. Supply external power.",
    ),
]


BUILT_IN_PRESETS: list[Profile] = (
    REGION_PRESETS + USE_CASE_PRESETS + MODEL_PRESETS + MAXIMIZE_PRESETS
)


def find_preset(name: str) -> Profile | None:
    for p in BUILT_IN_PRESETS:
        if p.name == name:
            return p
    return None


def presets_by_category(category: str) -> list[Profile]:
    return [p for p in BUILT_IN_PRESETS if p.category == category]


def presets_for_board(board_name: str, *, category: str | None = None) -> list[Profile]:
    """Filter presets to those whose applies_to matches `board_name`.

    Empty applies_to means "any board" and always passes the filter.
    """
    pool = (
        BUILT_IN_PRESETS if category is None
        else presets_by_category(category)
    )
    return [p for p in pool if p.matches_board(board_name)]
