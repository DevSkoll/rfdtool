"""Configuration validator for SiK / RFD900 S-register sets.

Pure-logic checker that turns a dict of S-register values (and optional pin
register values) into a :class:`ValidationReport` of
:class:`ValidationIssue` records the UI uses to tint individual rows and
to drive a summary dialog.

The validator composes 18 small rule functions (``_rule_1`` ..
``_rule_18``).  Each returns a list of issues; ``validate_config`` simply
concatenates them in a fixed order so the report can be reproduced
deterministically.

Two existing modules supply the ground truth:

* :mod:`rfd.registers` defines the per-register schema and the basic
  range/enum/bool validator used by rule R1.
* :mod:`rfd.regions` defines the regulatory regions (used by R8-R12) and
  the per-board TX-power and firmware-family lookups (used by R4 and R7).

There is intentionally no Qt or serial I/O in this module: it is safe to
import from both the protocol layer and the UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from . import registers
from .regions import (
    Region,
    detect_region,
    model_family,
    model_max_txpower,
)


# Air rates that look like "telemetry" links (R14 / R15 / R16 / R17 use
# this same notion of "telemetry-class" rates, ie. anything plausible for
# a MAVLink ground-station link rather than a fast point-to-point bridge).
_TELEMETRY_AIR_RATES: frozenset[int] = frozenset({64, 96, 128, 192, 200, 250})


@dataclass(frozen=True)
class ValidationIssue:
    severity: str                          # "error" | "warning" | "info"
    sregs: tuple[int, ...]                 # registers this issue concerns
    title: str                             # short label
    detail: str                            # 1-2 sentences of explanation
    fix_hint: str = ""                     # human-readable suggestion
    suggested_value: int | None = None     # for click-to-fix (paired with sregs[0])
    citation: str = ""                     # regulatory citation if applicable


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]
    detected_region: Region | None
    detected_board: str

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def infos(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "info"]

    @property
    def overall(self) -> str:
        """Worst severity present: 'errors' | 'warnings' | 'info' | 'ok'."""
        if self.errors:
            return "errors"
        if self.warnings:
            return "warnings"
        if self.infos:
            return "info"
        return "ok"

    @property
    def issues_by_sreg(self) -> dict[int, list[ValidationIssue]]:
        """Index issues by the registers they concern.

        An issue spanning multiple S-registers (e.g. R2 covering S8 and S9)
        appears under each key so the UI can tint every affected row.
        """
        out: dict[int, list[ValidationIssue]] = {}
        for issue in self.issues:
            for sreg in issue.sregs:
                out.setdefault(sreg, []).append(issue)
        return out


# --------------------------------------------------------------------- rules

def _rule_1(s_params: dict[int, int]) -> list[ValidationIssue]:
    """R1 — per-register range / enum / bool validation.

    Read-only registers (S0 FORMAT) are skipped: they're informational, the
    user isn't writing them, and the per-register validator returns "is
    read-only" for any value of theirs.
    """
    issues: list[ValidationIssue] = []
    for sreg in sorted(s_params):
        reg = registers.REGISTERS.get(sreg)
        if reg is not None and reg.read_only:
            continue
        value = s_params[sreg]
        ok, reason = registers.validate(sreg, value)
        if not ok:
            issues.append(ValidationIssue(
                severity="error",
                sregs=(sreg,),
                title=f"S{sreg}: invalid value {value}",
                detail=reason,
            ))
    return issues


def _rule_2(s_params: dict[int, int]) -> list[ValidationIssue]:
    """R2 — frequency ordering: MIN_FREQ must be strictly less than MAX_FREQ."""
    if 8 not in s_params or 9 not in s_params:
        return []
    s8, s9 = s_params[8], s_params[9]
    if s8 >= s9:
        return [ValidationIssue(
            severity="error",
            sregs=(8, 9),
            title="MIN_FREQ ≥ MAX_FREQ",
            detail="S8 must be strictly less than S9; the radio will refuse "
                   "the configuration.",
        )]
    return []


def _rule_3(s_params: dict[int, int]) -> list[ValidationIssue]:
    """R3 — channel spacing must be at least 50 kHz."""
    if not all(k in s_params for k in (8, 9, 10)):
        return []
    s8, s9, s10 = s_params[8], s_params[9], s_params[10]
    if s10 <= 0 or s8 >= s9:
        return []
    spacing = (s9 - s8) / s10
    if spacing < 50:
        return [ValidationIssue(
            severity="warning",
            sregs=(8, 9, 10),
            title="Channels too narrow",
            detail=(
                f"NUM_CHANNELS={s10} across {s9 - s8} kHz gives "
                f"{spacing:.0f} kHz/channel; SiK needs at least 50 kHz spacing."
            ),
        )]
    return []


def _rule_4(s_params: dict[int, int], board_name: str) -> list[ValidationIssue]:
    """R4 — TX power must not exceed the board's hardware ceiling."""
    if 4 not in s_params:
        return []
    cap = model_max_txpower(board_name)
    if cap is None:
        return []
    s4 = s_params[4]
    if s4 > cap:
        return [ValidationIssue(
            severity="error",
            sregs=(4,),
            title=f"TXPOWER {s4} dBm exceeds {board_name} maximum ({cap} dBm)",
            detail="The radio chip clips silently above this — the actual "
                   "output won't match the configured value.",
            fix_hint=f"Set S4 to {cap} or lower.",
            suggested_value=cap,
        )]
    return []


def _rule_5(s_params: dict[int, int]) -> list[ValidationIssue]:
    """R5 — 1 W output requires more current than a typical UART supplies.

    Info-not-warning: 30 dBm is legal in many regions and intentional on
    capable boards; we surface it so the integrator double-checks power.
    """
    if s_params.get(4) != 30:
        return []
    return [ValidationIssue(
        severity="info",
        sregs=(4,),
        title="1 W output requires external power",
        detail="30 dBm peak draws ≈2 A briefly; the typical autopilot "
               "telemetry port can't supply this. Use a separate ≥2 A "
               "regulator.",
    )]


def _rule_7(s_params: dict[int, int], board_name: str) -> list[ValidationIssue]:
    """R7 — newer air rates (200/224 kbps) aren't in the 8051 SiK table."""
    if 2 not in s_params:
        return []
    s2 = s_params[2]
    if s2 not in (200, 224):
        return []
    fam = model_family(board_name)
    if fam is None or fam.code != "8051":
        return []
    return [ValidationIssue(
        severity="warning",
        sregs=(2,),
        title=f"AIR_SPEED {s2} not in original SiK rate table",
        detail="The 8051 SiK firmware on this board may not support this "
               "air rate. Use 64 or 250 instead.",
    )]


def _rule_8(s_params: dict[int, int],
            region: Region | None) -> list[ValidationIssue]:
    """R8 — frequency range matches no known regulatory region."""
    s8 = s_params.get(8, 0)
    s9 = s_params.get(9, 0)
    if s8 <= 0 or s9 <= 0:
        return []
    if region is not None:
        return []
    return [ValidationIssue(
        severity="warning",
        sregs=(8, 9),
        title="Frequency range doesn't match any known region",
        detail="Verify legality with your local spectrum regulator before "
               "transmitting.",
    )]


def _rule_9(s_params: dict[int, int],
            region: Region | None) -> list[ValidationIssue]:
    """R9 — NUM_CHANNELS below regional FHSS minimum."""
    if region is None or region.min_channels is None:
        return []
    if 10 not in s_params:
        return []
    s10 = s_params[10]
    if s10 >= region.min_channels:
        return []
    return [ValidationIssue(
        severity="warning",
        sregs=(10,),
        title=(
            f"NUM_CHANNELS={s10} below {region.code} minimum "
            f"({region.min_channels})"
        ),
        detail=(
            f"{region.citation} requires at least {region.min_channels} "
            f"channels for FHSS compliance."
        ),
        suggested_value=region.min_channels,
        citation=region.citation,
    )]


def _rule_10(s_params: dict[int, int],
             region: Region | None) -> list[ValidationIssue]:
    """R10 — TX power above the regulatory limit for the matched region."""
    if region is None or region.max_tx_dbm is None:
        return []
    if 4 not in s_params:
        return []
    s4 = s_params[4]
    if s4 <= region.max_tx_dbm:
        return []
    return [ValidationIssue(
        severity="error",
        sregs=(4,),
        title=(
            f"TXPOWER {s4} dBm exceeds {region.code} regulatory limit "
            f"({region.max_tx_dbm} dBm)"
        ),
        detail=f"{region.citation} caps conducted TX power for this band.",
        suggested_value=region.max_tx_dbm,
        citation=region.citation,
    )]


def _rule_11(s_params: dict[int, int],
             region: Region | None) -> list[ValidationIssue]:
    """R11 — DUTY_CYCLE doesn't match the matched region."""
    if region is None or region.duty_cycle is None:
        return []
    if 11 not in s_params:
        return []
    s11 = s_params[11]
    if s11 == region.duty_cycle:
        return []
    return [ValidationIssue(
        severity="warning",
        sregs=(11,),
        title=(
            f"DUTY_CYCLE={s11} doesn't match {region.code} "
            f"({region.duty_cycle}%)"
        ),
        detail=(
            f"{region.citation} mandates a maximum duty cycle of "
            f"{region.duty_cycle}%."
        ),
        suggested_value=region.duty_cycle,
        citation=region.citation,
    )]


def _rule_12(s_params: dict[int, int],
             region: Region | None) -> list[ValidationIssue]:
    """R12 — LBT_RSSI below the matched region's minimum."""
    if region is None or region.lbt_rssi_min is None:
        return []
    if 12 not in s_params:
        return []
    s12 = s_params[12]
    if s12 >= region.lbt_rssi_min:
        return []
    return [ValidationIssue(
        severity="warning",
        sregs=(12,),
        title=(
            f"LBT_RSSI={s12} below {region.code} minimum "
            f"({region.lbt_rssi_min})"
        ),
        detail=(
            f"{region.citation} requires Listen-Before-Talk; S12 must "
            f"be ≥{region.lbt_rssi_min}."
        ),
        suggested_value=region.lbt_rssi_min,
        citation=region.citation,
    )]


def _rule_13(s_params: dict[int, int]) -> list[ValidationIssue]:
    """R13 — Golay ECC enabled (no longer recommended)."""
    if s_params.get(5) != 1:
        return []
    return [ValidationIssue(
        severity="warning",
        sregs=(5,),
        title="ECC enabled (no longer recommended)",
        detail="ArduPilot wiki: 'Using error correction is no longer "
               "recommended due to the range reduction and the fact that "
               "some newer radio chips are not capable of doing ECC.'",
        suggested_value=0,
    )]


def _rule_14(s_params: dict[int, int]) -> list[ValidationIssue]:
    """R14 — MAVLink framing disabled at a telemetry-class rate."""
    if 6 not in s_params or 2 not in s_params:
        return []
    if s_params[6] != 0:
        return []
    if s_params[2] not in _TELEMETRY_AIR_RATES:
        return []
    return [ValidationIssue(
        severity="info",
        sregs=(6,),
        title="MAVLink framing disabled",
        detail="At telemetry-class air rates, S6=2 (low-latency MAVLink) "
               "prevents packet fragmentation and prioritises RC_OVERRIDE. "
               "Set S6=0 only if you're bridging non-MAVLink serial.",
    )]


def _rule_15(s_params: dict[int, int]) -> list[ValidationIssue]:
    """R15 — MAVLink framing on a slow link saturates easily."""
    if 6 not in s_params or 2 not in s_params:
        return []
    s6 = s_params[6]
    s2 = s_params[2]
    if s6 < 1 or not (0 < s2 < 16):
        return []
    return [ValidationIssue(
        severity="warning",
        sregs=(2, 6),
        title="MAVLink framing on a slow link",
        detail="Air rates below 16 kbps are easily saturated by full-rate "
               "MAVLink. Reduce telemetry rates or raise AIR_SPEED.",
    )]


def _rule_16(s_params: dict[int, int]) -> list[ValidationIssue]:
    """R16 — small MAX_WINDOW combined with low AIR_SPEED starves throughput."""
    if 15 not in s_params or 2 not in s_params:
        return []
    s15 = s_params[15]
    s2 = s_params[2]
    if s15 >= 50 or not (0 < s2 < 64):
        return []
    return [ValidationIssue(
        severity="warning",
        sregs=(2, 15),
        title="Low MAX_WINDOW + low AIR_SPEED starves throughput",
        detail="Small windows minimise latency but require sufficient "
               "air-rate headroom. Either raise S2 to ≥64 or raise S15.",
    )]


def _rule_17(s_params: dict[int, int]) -> list[ValidationIssue]:
    """R17 — RTS/CTS rarely useful below 64 kbps air rate."""
    if 14 not in s_params or 2 not in s_params:
        return []
    if s_params[14] != 1 or not (0 < s_params[2] < 64):
        return []
    return [ValidationIssue(
        severity="info",
        sregs=(14,),
        title="RTS/CTS most useful at AIR_SPEED ≥ 64 kbps",
        detail="Hardware flow control protects against UART overrun mainly "
               "when the air link approaches the serial speed.",
    )]


def _rule_18(s_params: dict[int, int]) -> list[ValidationIssue]:
    """R18 — using factory NETID risks colliding with another nearby pair."""
    if s_params.get(3) != 25:
        return []
    return [ValidationIssue(
        severity="info",
        sregs=(3,),
        title="Using factory NETID (25)",
        detail="Pairs operating in proximity must each pick a unique NETID; "
               "otherwise hop sequences collide and ranges drop.",
    )]


# --------------------------------------------------------------------- entry point

def validate_config(
    s_params: dict[int, int],
    *,
    board_name: str = "",
    pin_params: dict[int, int] | None = None,
    is_remote: bool = False,
) -> ValidationReport:
    """Run every rule in canonical order and return a :class:`ValidationReport`.

    Set ``is_remote=True`` when validating the *remote* radio's panel: the
    partner's exact board model isn't reliably known, so model-specific
    rules (R4, R5, R7) are skipped.
    """
    # Deliberately do not mutate caller's dicts.
    s_params = dict(s_params)

    region: Region | None = None
    if 8 in s_params and 9 in s_params:
        region = detect_region(s_params[8], s_params[9])

    issues: list[ValidationIssue] = []
    issues.extend(_rule_1(s_params))
    issues.extend(_rule_2(s_params))
    issues.extend(_rule_3(s_params))
    if not is_remote:
        issues.extend(_rule_4(s_params, board_name))
        issues.extend(_rule_5(s_params))
        issues.extend(_rule_7(s_params, board_name))
    issues.extend(_rule_8(s_params, region))
    issues.extend(_rule_9(s_params, region))
    issues.extend(_rule_10(s_params, region))
    issues.extend(_rule_11(s_params, region))
    issues.extend(_rule_12(s_params, region))
    issues.extend(_rule_13(s_params))
    issues.extend(_rule_14(s_params))
    issues.extend(_rule_15(s_params))
    issues.extend(_rule_16(s_params))
    issues.extend(_rule_17(s_params))
    issues.extend(_rule_18(s_params))

    return ValidationReport(
        issues=tuple(issues),
        detected_region=region,
        detected_board=board_name,
    )
