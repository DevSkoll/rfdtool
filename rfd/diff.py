"""Pure-logic configuration diff used by the Compare dialog and the
pairing wizard.  No Qt — this is testable without a QApplication.

The diff is **name-keyed** so it works correctly across firmware variants
that put parameters at different sreg numbers (e.g. RFDesign 3.x putting
MAX_WINDOW at S14 while canonical SiK has it at S15).

Each entry carries the sreg number on each side as well, so the UI can
tint specific rows in the panel even if the two sides don't agree on
where a parameter lives.
"""
from __future__ import annotations

from dataclasses import dataclass


# Severity classes used to colour rows in the Compare dialog and to rank
# mismatches in the pairing wizard's diagnostic.
#
# - "rf"     — RF-critical: mismatch prevents a clean link or causes
#              partial-link symptoms (RSSI > 0 but pkts = 0).
# - "soft"   — Affects link quality but typically not the bind itself.
# - "uart"   — UART/GPIO/diagnostic; doesn't affect on-air link.
# - "missing-a" / "missing-b" — Parameter exists on one side but not the
#              other (e.g. canonical SiK MANCHESTER absent on RFDesign 3.x).
#              Treated as informational; the diff dialog renders these
#              with a "skip" hint rather than an error.

SEVERITY_RF = "rf"
SEVERITY_SOFT = "soft"
SEVERITY_UART = "uart"
SEVERITY_OK = "ok"
SEVERITY_MISSING_A = "missing-a"
SEVERITY_MISSING_B = "missing-b"


# Parameter name → severity if mismatched.  Anything not listed defaults
# to UART (non-RF).
SEVERITY_BY_NAME: dict[str, str] = {
    # RF-critical: must match end-to-end for clean bind.
    "NETID": SEVERITY_RF,
    "AIR_SPEED": SEVERITY_RF,
    "MIN_FREQ": SEVERITY_RF,
    "MAX_FREQ": SEVERITY_RF,
    "NUM_CHANNELS": SEVERITY_RF,
    "DUTY_CYCLE": SEVERITY_RF,
    "ECC": SEVERITY_RF,
    "MAVLINK": SEVERITY_RF,
    "MANCHESTER": SEVERITY_RF,
    "MAX_WINDOW": SEVERITY_RF,
    # Soft: affects link quality / behaviour but not the initial sync.
    "RTSCTS": SEVERITY_SOFT,
    "OPPRESEND": SEVERITY_SOFT,
    "LBT_RSSI": SEVERITY_SOFT,
    "AIR_FRAMELEN": SEVERITY_SOFT,
    "TXPOWER": SEVERITY_SOFT,
    # UART / GPIO / diagnostic — explicitly listed for clarity even where
    # the default would already classify them correctly.
    "SERIAL_SPEED": SEVERITY_UART,
    "AUXSER_SPEED": SEVERITY_UART,
    "FSFRAMELOSS": SEVERITY_UART,
    "RSSI_IN_DBM": SEVERITY_UART,
    "ANT_MODE": SEVERITY_UART,
    "ENCRYPTION_LEVEL": SEVERITY_UART,
    "FORMAT": SEVERITY_UART,
}


def severity_for(name: str) -> str:
    """Severity of a *mismatch* on parameter `name`.  Default: UART."""
    if name.startswith("GPI") or name.startswith("GPO"):
        return SEVERITY_UART
    return SEVERITY_BY_NAME.get(name, SEVERITY_UART)


# Severity ordering used to sort and colour rows.  Higher = more urgent.
_SEVERITY_RANK = {
    SEVERITY_RF: 4,
    SEVERITY_MISSING_A: 3,
    SEVERITY_MISSING_B: 3,
    SEVERITY_SOFT: 2,
    SEVERITY_UART: 1,
    SEVERITY_OK: 0,
}


@dataclass(frozen=True)
class CompareEntry:
    name: str                     # parameter name (canonical)
    severity: str                 # see SEVERITY_* constants
    sreg_a: int | None            # sreg on side A (None if missing)
    sreg_b: int | None            # sreg on side B (None if missing)
    value_a: int | None
    value_b: int | None

    @property
    def is_match(self) -> bool:
        return self.severity == SEVERITY_OK

    @property
    def is_rf_critical(self) -> bool:
        return self.severity == SEVERITY_RF

    @property
    def is_missing(self) -> bool:
        return self.severity in (SEVERITY_MISSING_A, SEVERITY_MISSING_B)


def diff_configs(
    a_params: dict[str, int],
    b_params: dict[str, int],
    *,
    a_sregs: dict[str, int] | None = None,
    b_sregs: dict[str, int] | None = None,
) -> list[CompareEntry]:
    """Compute a name-keyed diff between two configurations.

    ``a_params`` / ``b_params`` map parameter name → value.  The optional
    ``a_sregs`` / ``b_sregs`` (name → sreg) let the UI know which row to
    highlight on each side.  When omitted, the entry's ``sreg_a`` /
    ``sreg_b`` will be None.

    Result is sorted by severity (most urgent first), then alphabetically
    by name.
    """
    a_sregs = dict(a_sregs or {})
    b_sregs = dict(b_sregs or {})
    all_names = sorted(set(a_params.keys()) | set(b_params.keys()))

    out: list[CompareEntry] = []
    for name in all_names:
        in_a = name in a_params
        in_b = name in b_params
        sreg_a = a_sregs.get(name)
        sreg_b = b_sregs.get(name)
        value_a = a_params.get(name)
        value_b = b_params.get(name)
        if in_a and not in_b:
            severity = SEVERITY_MISSING_B
        elif in_b and not in_a:
            severity = SEVERITY_MISSING_A
        elif value_a == value_b:
            severity = SEVERITY_OK
        else:
            severity = severity_for(name)
        out.append(CompareEntry(
            name=name, severity=severity,
            sreg_a=sreg_a, sreg_b=sreg_b,
            value_a=value_a, value_b=value_b,
        ))

    out.sort(key=lambda e: (-_SEVERITY_RANK.get(e.severity, 0), e.name))
    return out


def summarise(entries: list[CompareEntry]) -> dict[str, int]:
    """Quick count of entries per severity for a header line."""
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.severity] = counts.get(e.severity, 0) + 1
    return counts
