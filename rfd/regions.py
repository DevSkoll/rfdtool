"""Regional regulatory data for RFD900-series radios.

Single source of truth used by both the preset library (to build
region-specific presets) and the validator (to flag illegal/suboptimal
configurations).  Pulled verbatim from the ArduPilot wiki's "Telemetry
Radio Regional Regulations" page plus the RFDesign datasheets and the
ETSI / FCC source documents cited inline.

This module is pure data + small lookup helpers; no I/O, no Qt.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Region:
    code: str                  # short identifier ("US", "AU", "EU433", …)
    name: str                  # human-readable label for menus/dialogs
    citation: str              # regulatory citation
    min_freq: int              # kHz, inclusive
    max_freq: int              # kHz, inclusive
    min_channels: int | None   # regulatory minimum NUM_CHANNELS, or None
    max_tx_dbm: int | None     # regulatory max TX power in dBm, or None
    duty_cycle: int | None     # required DUTY_CYCLE %, or None for "100"
    lbt_rssi_min: int | None   # required minimum LBT_RSSI threshold
    notes: str = ""

    def covers(self, s8: int, s9: int) -> bool:
        """True if (S8, S9) falls entirely within this region's allowed range."""
        return s8 >= self.min_freq and s9 <= self.max_freq


# Order matters for tie-breaking in detect_region(): more-restrictive ranges
# should come *after* more-permissive ones with the same band, since we sort
# candidates by range width and pick the narrowest match.
REGIONS: list[Region] = [
    Region(
        code="US",
        name="United States / Canada (902–928 MHz)",
        citation="FCC 15.247 / IC RSS-210 Annex 8.1",
        min_freq=902000, max_freq=928000,
        min_channels=50, max_tx_dbm=30, duty_cycle=100, lbt_rssi_min=None,
    ),
    Region(
        code="AU",
        name="Australia (915–928 MHz)",
        citation="ACMA LIPD-2015",
        min_freq=915001, max_freq=928000,
        min_channels=20, max_tx_dbm=30, duty_cycle=100, lbt_rssi_min=None,
        notes="≤1 W EIRP including antenna gain.",
    ),
    Region(
        code="NZ",
        name="New Zealand (921–928 MHz)",
        citation="NZ General User Radio Licence — Notice 2007 Schedule 1",
        min_freq=921000, max_freq=928000,
        min_channels=20, max_tx_dbm=None, duty_cycle=100, lbt_rssi_min=None,
    ),
    Region(
        code="BR",
        name="Brazil (915–928 MHz)",
        citation="ANATEL Resolução 506/2008",
        min_freq=915000, max_freq=928000,
        min_channels=26, max_tx_dbm=None, duty_cycle=100, lbt_rssi_min=None,
    ),
    Region(
        code="AR",
        name="Argentina (902–928 MHz)",
        citation="ENACOM Resolución SC 302/98",
        min_freq=902000, max_freq=928000,
        min_channels=50, max_tx_dbm=None, duty_cycle=100, lbt_rssi_min=None,
    ),
    Region(
        code="EU433",
        name="EU / UK 433 MHz ISM",
        citation="ETSI EN 300 220 V3.1.1 / OFCOM IR2030/1/10",
        min_freq=433050, max_freq=434790,
        min_channels=None, max_tx_dbm=7, duty_cycle=10, lbt_rssi_min=25,
        notes="10% duty cycle, LBT required, ≤7 dBm conducted.",
    ),
    Region(
        code="AU433",
        name="Australia 433 MHz",
        citation="ACMA LIPD",
        min_freq=433051, max_freq=434790,
        min_channels=None, max_tx_dbm=14, duty_cycle=100, lbt_rssi_min=None,
        notes="≤25 mW EIRP (≈14 dBm).",
    ),
    Region(
        code="ZA433",
        name="South Africa 433 MHz",
        citation="ICASA",
        min_freq=433050, max_freq=434790,
        min_channels=None, max_tx_dbm=10, duty_cycle=None, lbt_rssi_min=None,
        notes="≤10 mW EIRP.",
    ),
]


def detect_region(s8: int, s9: int) -> Region | None:
    """Match a (MIN_FREQ, MAX_FREQ) configuration to a regulatory region.

    A user's range may fit inside several regions (e.g. 915–928 MHz fits both
    the US 902–928 envelope and the AU 915–928 envelope).  We return the
    *narrowest* matching region, since that's the one the user is most
    likely targeting.  Returns None if no region covers the range.
    """
    if s8 <= 0 or s9 <= 0 or s8 >= s9:
        return None
    candidates = [r for r in REGIONS if r.covers(s8, s9)]
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.max_freq - r.min_freq)
    return candidates[0]


def find_region(code: str) -> Region | None:
    for r in REGIONS:
        if r.code == code:
            return r
    return None


# --------------------------------------------------------------------- model TX limits
# Per-board TX power ceilings (dBm) — based on RFDesign datasheets and
# community-validated reports.  Boards above these limits silently clip.
MODEL_MAX_TXPOWER: dict[str, int] = {
    "RFD900":   20,
    "RFD900a":  20,
    "RFD900u":  20,
    "RFD900p":  27,    # RFD900+ tops out at 27 dBm (≈500 mW)
    "RFD900x":  30,
    "RFD900ux": 30,
    "RFD900x2": 30,
}


def model_max_txpower(board_name: str) -> int | None:
    """Return the TX-power ceiling for `board_name`, or None if unknown.

    Matches by case-insensitive substring so banner-style strings like
    'RFD900X2-US' still resolve.
    """
    if not board_name:
        return None
    lowered = board_name.lower()
    # Prefer the most-specific (longest) name that's a substring of board_name
    matches = [(name, cap) for name, cap in MODEL_MAX_TXPOWER.items()
               if name.lower() in lowered]
    if not matches:
        return None
    matches.sort(key=lambda kv: -len(kv[0]))
    return matches[0][1]


# --------------------------------------------------------------------- model families
@dataclass(frozen=True)
class ModelFamily:
    code: str               # "8051", "stm32_v1", "stm32_v2"
    name: str
    boards: tuple[str, ...]


MODEL_FAMILIES: list[ModelFamily] = [
    ModelFamily("8051", "RFD900 / RFD900+ / RFD900u (8051)",
                ("RFD900", "RFD900a", "RFD900p", "RFD900u")),
    ModelFamily("stm32_v1", "RFD900x / RFD900ux (STM32, SiK 2.x)",
                ("RFD900x", "RFD900ux")),
    ModelFamily("stm32_v2", "RFD900x2 (STM32, SiK 3.x)",
                ("RFD900x2",)),
]


def model_family(board_name: str) -> ModelFamily | None:
    if not board_name:
        return None
    lowered = board_name.lower()
    # Prefer the family whose most-specific board name matches — sort all
    # candidate (family, board) pairs by board-name length descending so
    # "RFD900x2" beats "RFD900".
    matches: list[tuple[int, ModelFamily]] = []
    for fam in MODEL_FAMILIES:
        for b in fam.boards:
            if b.lower() in lowered:
                matches.append((len(b), fam))
    if not matches:
        return None
    matches.sort(key=lambda kv: -kv[0])
    return matches[0][1]
