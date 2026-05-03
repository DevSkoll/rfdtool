"""Higher-level configuration validation rules for SiK-family radios.

Operates in **parameter-name space** rather than sreg-number space, so the
same rule fires correctly regardless of which sreg a particular firmware
puts a parameter at.  RFDesign's SiK 3.x reorders S13/S14/S15 relative to
canonical SiK; both layouts go through the same rules here as long as the
caller passes the firmware's ``sreg → name`` map (typically built from the
firmware's own ``ATI5`` response).

The validator composes 19 small rule functions (``_rule_1`` ..
``_rule_19``).  Each returns a list of issues; ``validate_config`` simply
chains them.  ``ValidationIssue.sregs`` is still populated for the
existing UI; ``param_names`` carries the canonical name of each register
the issue concerns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import registers, regions
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
    sregs: tuple[int, ...]                 # firmware-specific sreg numbers
    title: str                             # short label
    detail: str                            # 1-2 sentences of explanation
    fix_hint: str = ""                     # human-readable suggestion
    suggested_value: int | None = None     # for click-to-fix
    citation: str = ""                     # regulatory citation if applicable
    param_names: tuple[str, ...] = ()      # canonical parameter names


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

    @property
    def issues_by_name(self) -> dict[str, list[ValidationIssue]]:
        """Index issues by canonical parameter name (where known)."""
        out: dict[str, list[ValidationIssue]] = {}
        for issue in self.issues:
            for name in issue.param_names:
                out.setdefault(name, []).append(issue)
        return out


# --------------------------------------------------------------------- helpers
def _sreg_label(sreg: int | None, name: str) -> str:
    return f"S{sreg} {name}" if sreg is not None else name


def _make_issue(
    *,
    severity: str,
    names: tuple[str, ...],
    name_to_sreg: dict[str, int],
    title: str,
    detail: str,
    fix_hint: str = "",
    suggested_value: int | None = None,
    citation: str = "",
) -> ValidationIssue:
    sregs = tuple(name_to_sreg[n] for n in names if n in name_to_sreg)
    return ValidationIssue(
        severity=severity,
        sregs=sregs,
        title=title,
        detail=detail,
        fix_hint=fix_hint,
        suggested_value=suggested_value,
        citation=citation,
        param_names=names,
    )


# --------------------------------------------------------------------- rules
# Each rule operates on a name-keyed view of the configuration.  Issues
# carry both the canonical name(s) and the sreg numbers (translated via
# `name_to_sreg`) so the UI can tint the right rows regardless of which
# sreg the firmware happens to use for that parameter.

def _rule_1(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R1 — per-parameter range / enum / bool validation.

    Read-only parameters (e.g. FORMAT) are skipped — the user isn't
    writing them, and the per-name validator returns "is read-only" for
    any value of theirs.  Names absent from the catalog are accepted (the
    firmware says they exist; we don't have an opinion on their range).
    """
    issues: list[ValidationIssue] = []
    for name in sorted(by_name):
        spec = registers.get_spec(name)
        if spec is not None and spec.read_only:
            continue
        value = by_name[name]
        ok, reason = registers.validate_value(name, value)
        if not ok:
            sreg = name_to_sreg.get(name)
            issues.append(ValidationIssue(
                severity="error",
                sregs=(sreg,) if sreg is not None else (),
                title=f"{_sreg_label(sreg, name)}: invalid value {value}",
                detail=reason,
                param_names=(name,),
            ))
    return issues


def _rule_2(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R2 — frequency ordering: MIN_FREQ must be strictly less than MAX_FREQ."""
    if "MIN_FREQ" not in by_name or "MAX_FREQ" not in by_name:
        return []
    if by_name["MIN_FREQ"] >= by_name["MAX_FREQ"]:
        return [_make_issue(
            severity="error",
            names=("MIN_FREQ", "MAX_FREQ"),
            name_to_sreg=name_to_sreg,
            title="MIN_FREQ ≥ MAX_FREQ",
            detail="MIN_FREQ must be strictly less than MAX_FREQ; the "
                   "radio will refuse the configuration.",
        )]
    return []


def _rule_3(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R3 — channel spacing must be at least 50 kHz."""
    if not all(n in by_name for n in ("MIN_FREQ", "MAX_FREQ", "NUM_CHANNELS")):
        return []
    s8 = by_name["MIN_FREQ"]
    s9 = by_name["MAX_FREQ"]
    s10 = by_name["NUM_CHANNELS"]
    if s10 <= 0 or s8 >= s9:
        return []
    spacing = (s9 - s8) / s10
    if spacing < 50:
        return [_make_issue(
            severity="warning",
            names=("MIN_FREQ", "MAX_FREQ", "NUM_CHANNELS"),
            name_to_sreg=name_to_sreg,
            title="Channels too narrow",
            detail=(
                f"NUM_CHANNELS={s10} across {s9 - s8} kHz gives "
                f"{spacing:.0f} kHz/channel; SiK needs at least 50 kHz spacing."
            ),
        )]
    return []


def _rule_4(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    board_name: str,
) -> list[ValidationIssue]:
    """R4 — TX power must not exceed the board's hardware ceiling."""
    if "TXPOWER" not in by_name:
        return []
    cap = model_max_txpower(board_name)
    if cap is None:
        return []
    s4 = by_name["TXPOWER"]
    if s4 > cap:
        return [_make_issue(
            severity="error",
            names=("TXPOWER",),
            name_to_sreg=name_to_sreg,
            title=f"TXPOWER {s4} dBm exceeds {board_name} maximum ({cap} dBm)",
            detail="The radio chip clips silently above this — the actual "
                   "output won't match the configured value.",
            fix_hint=f"Set TXPOWER to {cap} or lower.",
            suggested_value=cap,
        )]
    return []


def _rule_5(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R5 — 1 W output requires more current than a typical UART supplies."""
    if by_name.get("TXPOWER") != 30:
        return []
    return [_make_issue(
        severity="info",
        names=("TXPOWER",),
        name_to_sreg=name_to_sreg,
        title="1 W output requires external power",
        detail="30 dBm peak draws ≈2 A briefly; the typical autopilot "
               "telemetry port can't supply this. Use a separate ≥2 A "
               "regulator.",
    )]


def _rule_7(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    board_name: str,
) -> list[ValidationIssue]:
    """R7 — newer air rates (200/224 kbps) aren't in the 8051 SiK table."""
    if "AIR_SPEED" not in by_name:
        return []
    s2 = by_name["AIR_SPEED"]
    if s2 not in (200, 224):
        return []
    fam = model_family(board_name)
    if fam is None or fam.code != "8051":
        return []
    return [_make_issue(
        severity="warning",
        names=("AIR_SPEED",),
        name_to_sreg=name_to_sreg,
        title=f"AIR_SPEED {s2} not in original SiK rate table",
        detail="The 8051 SiK firmware on this board may not support this "
               "air rate. Use 64 or 250 instead.",
    )]


def _rule_8(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    region: Region | None,
) -> list[ValidationIssue]:
    """R8 — frequency range matches no known regulatory region."""
    s8 = by_name.get("MIN_FREQ", 0)
    s9 = by_name.get("MAX_FREQ", 0)
    if s8 <= 0 or s9 <= 0:
        return []
    if region is not None:
        return []
    return [_make_issue(
        severity="warning",
        names=("MIN_FREQ", "MAX_FREQ"),
        name_to_sreg=name_to_sreg,
        title="Frequency range doesn't match any known region",
        detail="Verify legality with your local spectrum regulator before "
               "transmitting.",
    )]


def _rule_9(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    region: Region | None,
) -> list[ValidationIssue]:
    """R9 — NUM_CHANNELS below regional FHSS minimum."""
    if region is None or region.min_channels is None:
        return []
    if "NUM_CHANNELS" not in by_name:
        return []
    s10 = by_name["NUM_CHANNELS"]
    if s10 >= region.min_channels:
        return []
    return [_make_issue(
        severity="warning",
        names=("NUM_CHANNELS",),
        name_to_sreg=name_to_sreg,
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


def _rule_10(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    region: Region | None,
) -> list[ValidationIssue]:
    """R10 — TX power above the regulatory limit for the matched region."""
    if region is None or region.max_tx_dbm is None:
        return []
    if "TXPOWER" not in by_name:
        return []
    s4 = by_name["TXPOWER"]
    if s4 <= region.max_tx_dbm:
        return []
    return [_make_issue(
        severity="error",
        names=("TXPOWER",),
        name_to_sreg=name_to_sreg,
        title=(
            f"TXPOWER {s4} dBm exceeds {region.code} regulatory limit "
            f"({region.max_tx_dbm} dBm)"
        ),
        detail=f"{region.citation} caps conducted TX power for this band.",
        suggested_value=region.max_tx_dbm,
        citation=region.citation,
    )]


def _rule_11(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    region: Region | None,
) -> list[ValidationIssue]:
    """R11 — DUTY_CYCLE doesn't match the matched region."""
    if region is None or region.duty_cycle is None:
        return []
    if "DUTY_CYCLE" not in by_name:
        return []
    s11 = by_name["DUTY_CYCLE"]
    if s11 == region.duty_cycle:
        return []
    return [_make_issue(
        severity="warning",
        names=("DUTY_CYCLE",),
        name_to_sreg=name_to_sreg,
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


def _rule_12(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
    region: Region | None,
) -> list[ValidationIssue]:
    """R12 — LBT_RSSI below the matched region's minimum."""
    if region is None or region.lbt_rssi_min is None:
        return []
    if "LBT_RSSI" not in by_name:
        return []
    s12 = by_name["LBT_RSSI"]
    if s12 >= region.lbt_rssi_min:
        return []
    return [_make_issue(
        severity="warning",
        names=("LBT_RSSI",),
        name_to_sreg=name_to_sreg,
        title=(
            f"LBT_RSSI={s12} below {region.code} minimum "
            f"({region.lbt_rssi_min})"
        ),
        detail=(
            f"{region.citation} requires Listen-Before-Talk; LBT_RSSI "
            f"must be ≥{region.lbt_rssi_min}."
        ),
        suggested_value=region.lbt_rssi_min,
        citation=region.citation,
    )]


def _rule_13(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R13 — Golay ECC enabled (no longer recommended)."""
    if by_name.get("ECC") != 1:
        return []
    return [_make_issue(
        severity="warning",
        names=("ECC",),
        name_to_sreg=name_to_sreg,
        title="ECC enabled (no longer recommended)",
        detail="ArduPilot wiki: 'Using error correction is no longer "
               "recommended due to the range reduction and the fact that "
               "some newer radio chips are not capable of doing ECC.'",
        suggested_value=0,
    )]


def _rule_14(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R14 — MAVLink framing disabled at a telemetry-class rate."""
    if "MAVLINK" not in by_name or "AIR_SPEED" not in by_name:
        return []
    if by_name["MAVLINK"] != 0:
        return []
    if by_name["AIR_SPEED"] not in _TELEMETRY_AIR_RATES:
        return []
    return [_make_issue(
        severity="info",
        names=("MAVLINK",),
        name_to_sreg=name_to_sreg,
        title="MAVLink framing disabled",
        detail="At telemetry-class air rates, MAVLINK=2 (low-latency) "
               "prevents packet fragmentation and prioritises RC_OVERRIDE. "
               "Set MAVLINK=0 only if you're bridging non-MAVLink serial.",
    )]


def _rule_15(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R15 — MAVLink framing on a slow link saturates easily."""
    if "MAVLINK" not in by_name or "AIR_SPEED" not in by_name:
        return []
    s6 = by_name["MAVLINK"]
    s2 = by_name["AIR_SPEED"]
    if s6 < 1 or not (0 < s2 < 16):
        return []
    return [_make_issue(
        severity="warning",
        names=("AIR_SPEED", "MAVLINK"),
        name_to_sreg=name_to_sreg,
        title="MAVLink framing on a slow link",
        detail="Air rates below 16 kbps are easily saturated by full-rate "
               "MAVLink. Reduce telemetry rates or raise AIR_SPEED.",
    )]


def _rule_16(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R16 — small MAX_WINDOW combined with low AIR_SPEED starves throughput."""
    if "MAX_WINDOW" not in by_name or "AIR_SPEED" not in by_name:
        return []
    s15 = by_name["MAX_WINDOW"]
    s2 = by_name["AIR_SPEED"]
    if s15 >= 50 or not (0 < s2 < 64):
        return []
    return [_make_issue(
        severity="warning",
        names=("AIR_SPEED", "MAX_WINDOW"),
        name_to_sreg=name_to_sreg,
        title="Low MAX_WINDOW + low AIR_SPEED starves throughput",
        detail="Small windows minimise latency but require sufficient "
               "air-rate headroom. Either raise AIR_SPEED to ≥64 or "
               "raise MAX_WINDOW.",
    )]


def _rule_17(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R17 — RTS/CTS rarely useful below 64 kbps air rate."""
    if "RTSCTS" not in by_name or "AIR_SPEED" not in by_name:
        return []
    if by_name["RTSCTS"] != 1 or not (0 < by_name["AIR_SPEED"] < 64):
        return []
    return [_make_issue(
        severity="info",
        names=("RTSCTS",),
        name_to_sreg=name_to_sreg,
        title="RTS/CTS most useful at AIR_SPEED ≥ 64 kbps",
        detail="Hardware flow control protects against UART overrun mainly "
               "when the air link approaches the serial speed.",
    )]


def _rule_18(
    by_name: dict[str, int],
    name_to_sreg: dict[str, int],
) -> list[ValidationIssue]:
    """R18 — using factory NETID risks colliding with another nearby pair."""
    if by_name.get("NETID") != 25:
        return []
    return [_make_issue(
        severity="info",
        names=("NETID",),
        name_to_sreg=name_to_sreg,
        title="Using factory NETID (25)",
        detail="Pairs operating in proximity must each pick a unique NETID; "
               "otherwise hop sequences collide and ranges drop.",
    )]


def _rule_19(
    s_params: dict[int, int],
    current_values: dict[int, int],
    sreg_to_name: dict[int, str],
    firmware_banner: str,
) -> list[ValidationIssue]:
    """R19 — firmware-locked register: pre-flight detection.

    Some RFDesign firmware variants (notably "-US"/"-EU" SKUs) factory-lock
    a subset of S-registers to keep the radio inside its certification
    envelope.  The radio replies "ERROR" to *any* attempted change of those
    registers.  This rule operates in *sreg* space because the lock is
    enforced by the firmware on the wire — but the issue title surfaces
    the firmware's reported parameter NAME so the user sees what the
    register actually represents on their radio.
    """
    locked = regions.firmware_lockdown(firmware_banner)
    if not locked:
        return []
    label = regions.lockdown_label(firmware_banner) or "this firmware variant"
    out: list[ValidationIssue] = []
    for sreg in sorted(locked):
        if sreg not in s_params or sreg not in current_values:
            continue
        intended = s_params[sreg]
        actual = current_values[sreg]
        if intended == actual:
            continue
        name = sreg_to_name.get(sreg, "")
        display = f"S{sreg} {name}".strip() or f"S{sreg}"
        out.append(ValidationIssue(
            severity="warning",
            sregs=(sreg,),
            param_names=(name,) if name else (),
            title=f"{display} is firmware-locked on {label}",
            detail=(
                f"Your intended value ({intended}) differs from the radio's "
                f"current value ({actual}). RFDesign locks frequency-hopping "
                f"and air-rate parameters on certified region SKUs to keep "
                f"the radio inside its FCC/ETSI/ACMA test envelope. Save will "
                f"fail for this register; revert to {actual} or leave it as-is."
            ),
            fix_hint=f"Revert to the radio's locked value ({actual}).",
            suggested_value=actual,
        ))
    return out


# --------------------------------------------------------------------- entry point

def validate_config(
    s_params: dict[int, int],
    *,
    sreg_to_name: dict[int, str] | None = None,
    board_name: str = "",
    pin_params: dict[int, int] | None = None,
    is_remote: bool = False,
    firmware_banner: str = "",
    current_values: dict[int, int] | None = None,
) -> ValidationReport:
    """Run every rule in canonical order and return a :class:`ValidationReport`.

    ``s_params`` and ``current_values`` are sreg-keyed (matching what the
    UI tracks).  ``sreg_to_name`` tells us which sreg holds which parameter
    on this firmware — typically the ``s_names`` field of the radio's
    ``ATI5`` response.  When omitted the canonical SiK mapping is used,
    which matches the original firmware but not RFDesign's SiK 3.x.

    ``firmware_banner`` and ``current_values`` are inputs to R19
    (firmware-lockdown detection); without them R19 is silently skipped.

    Set ``is_remote=True`` when validating the *remote* radio's panel —
    model-specific rules (R4, R5, R7, R19) are skipped because the partner
    radio's firmware variant isn't reliably known.
    """
    if sreg_to_name is None:
        sreg_to_name = registers.CANONICAL_SIK_NAMES
    name_to_sreg: dict[str, int] = {n: s for s, n in sreg_to_name.items()}

    # Project sreg-keyed inputs onto the name-keyed view rules use.
    by_name: dict[str, int] = {}
    for sreg, value in s_params.items():
        name = sreg_to_name.get(sreg)
        if name:
            by_name[name] = value

    region: Region | None = None
    if "MIN_FREQ" in by_name and "MAX_FREQ" in by_name:
        region = detect_region(by_name["MIN_FREQ"], by_name["MAX_FREQ"])

    issues: list[ValidationIssue] = []
    issues.extend(_rule_1(by_name, name_to_sreg))
    issues.extend(_rule_2(by_name, name_to_sreg))
    issues.extend(_rule_3(by_name, name_to_sreg))
    if not is_remote:
        issues.extend(_rule_4(by_name, name_to_sreg, board_name))
        issues.extend(_rule_5(by_name, name_to_sreg))
        issues.extend(_rule_7(by_name, name_to_sreg, board_name))
    issues.extend(_rule_8(by_name, name_to_sreg, region))
    issues.extend(_rule_9(by_name, name_to_sreg, region))
    issues.extend(_rule_10(by_name, name_to_sreg, region))
    issues.extend(_rule_11(by_name, name_to_sreg, region))
    issues.extend(_rule_12(by_name, name_to_sreg, region))
    issues.extend(_rule_13(by_name, name_to_sreg))
    issues.extend(_rule_14(by_name, name_to_sreg))
    issues.extend(_rule_15(by_name, name_to_sreg))
    issues.extend(_rule_16(by_name, name_to_sreg))
    issues.extend(_rule_17(by_name, name_to_sreg))
    issues.extend(_rule_18(by_name, name_to_sreg))
    if not is_remote and current_values:
        issues.extend(_rule_19(
            dict(s_params), dict(current_values), sreg_to_name, firmware_banner,
        ))

    return ValidationReport(
        issues=tuple(issues),
        detected_region=region,
        detected_board=board_name,
    )
