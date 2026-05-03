"""SiK firmware S-register and pin-mapping (R-register) definitions.

The SiK firmware family (used by the RFDesign RFD900x/900ux/900+ and similar
HopeRF/HM-TRP modems) exposes its persistent configuration as numbered
"S-registers" (S0..S15) plus, on RFD900x/ux, a set of pin-mapping registers
(R0..R15). The radio reads/writes them with ``ATSn?`` and ``ATSn=value``
commands; values survive a reboot once committed with ``AT&W``.

This module is pure data plus a tiny validator. It deliberately performs no
serial I/O and imports nothing outside the standard library so it is safe to
import from both the protocol layer and the Qt UI without side effects.

For the canonical register semantics see the SiK firmware source:
    https://github.com/RFDesign/SiK
specifically ``Firmware/radio/parameters.h`` and ``Firmware/radio/parameters.c``.
The RFD900x extensions (pin-mapping, additional air rates) are documented in
the RFD900x manual published by RFDesign.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegisterDef:
    sreg: int
    name: str
    label: str
    tooltip: str
    kind: str
    minimum: int | None
    maximum: int | None
    enum: dict[int, str] | None
    default: int | None
    units: str | None
    variant_notes: str = ""
    read_only: bool = False


# SiK accepts a shorthand for serial baud rates: the integer is the rate in
# kbps truncated (e.g. 57 -> 57600). Keep the mapping aligned with what the
# firmware's parameter table accepts so the UI's combo entries round-trip.
_SERIAL_SPEED_ENUM: dict[int, str] = {
    1: "1200",
    2: "2400",
    4: "4800",
    9: "9600",
    19: "19200",
    38: "38400",
    57: "57600",
    115: "115200",
    230: "230400",
}

# S2 AIR_SPEED is encoded as the SUPERSET of values supported across the
# RFD900/900+ and RFD900x/ux variants. The classic RFD900/900+ supports rates
# up to 250 kbps; the RFD900x firmware exposes a different curated subset
# (12/56/64/100/125/200/250). The UI must be able to display whatever is read
# from the radio regardless of which firmware generated it; per-variant
# legality is enforced at runtime by the layer that knows which radio is
# attached, not by this validator.
_AIR_SPEED_VALUES: tuple[int, ...] = (
    2, 4, 8, 16, 19, 24, 32, 48, 64, 96, 128, 192, 200, 224, 250,
)
_AIR_SPEED_ENUM: dict[int, str] = {v: f"{v} kbps" for v in _AIR_SPEED_VALUES}


REGISTERS: dict[int, RegisterDef] = {
    0: RegisterDef(
        sreg=0,
        name="FORMAT",
        label="Parameter format",
        tooltip="Parameter format version reported by the firmware. "
                "Read-only; written by the radio itself.",
        kind="int",
        minimum=None,
        maximum=None,
        enum=None,
        default=None,
        units=None,
        read_only=True,
    ),
    1: RegisterDef(
        sreg=1,
        name="SERIAL_SPEED",
        label="Serial baud rate",
        tooltip="Baud rate of the host UART, expressed as the SiK shorthand "
                "(e.g. 57 = 57600 baud). Both ends of the link must agree "
                "with the host that talks to them; the over-the-air rate is "
                "set separately by AIR_SPEED.",
        kind="enum",
        minimum=None,
        maximum=None,
        enum=dict(_SERIAL_SPEED_ENUM),
        default=57,
        units="kbps",
    ),
    2: RegisterDef(
        sreg=2,
        name="AIR_SPEED",
        label="Air data rate",
        tooltip="Over-the-air data rate in kbps. Lower rates trade throughput "
                "for range and link margin. Both ends of the link must use "
                "the same value.",
        kind="enum",
        minimum=None,
        maximum=None,
        enum=dict(_AIR_SPEED_ENUM),
        default=64,
        units="kbps",
        variant_notes=(
            "RFD900/900+ supports rates up to 250 kbps from the full table. "
            "RFD900x exposes a curated subset (typically 12/56/64/100/125/"
            "200/250). This module encodes the superset so the UI can render "
            "any value read from the radio; per-variant legality is enforced "
            "elsewhere."
        ),
    ),
    3: RegisterDef(
        sreg=3,
        name="NETID",
        label="Network ID",
        tooltip="Network identifier. Both ends of a link must share the same "
                "NETID; radios on different NETIDs ignore each other.",
        kind="int",
        minimum=0,
        maximum=499,
        enum=None,
        default=25,
        units=None,
    ),
    4: RegisterDef(
        sreg=4,
        name="TXPOWER",
        label="Transmit power",
        tooltip="Transmit power in dBm. Higher values increase range at the "
                "cost of current draw and regulatory headroom.",
        kind="int",
        minimum=0,
        maximum=30,
        enum=None,
        default=20,
        units="dBm",
        variant_notes="RFD900x can output up to 30 dBm (1 W); some variants cap lower.",
    ),
    5: RegisterDef(
        sreg=5,
        name="ECC",
        label="Error correction (Golay)",
        tooltip="Enables Golay forward error correction. Halves usable "
                "throughput but greatly improves reliability on marginal links.",
        kind="bool",
        minimum=None,
        maximum=None,
        enum={0: "Off", 1: "On"},
        default=0,
        units=None,
    ),
    6: RegisterDef(
        sreg=6,
        name="MAVLINK",
        label="MAVLink framing",
        tooltip="Selects how the radio frames the serial stream. MAVLink "
                "framing aligns radio packets to MAVLink message boundaries; "
                "low-latency MAVLink shortens TDM slots for latency-sensitive "
                "links.",
        kind="enum",
        minimum=None,
        maximum=None,
        enum={0: "Raw data", 1: "MAVLink framing", 2: "Low-latency MAVLink"},
        default=1,
        units=None,
    ),
    7: RegisterDef(
        sreg=7,
        name="OPPRESEND",
        label="Opportunistic resend",
        tooltip="When enabled, the radio uses otherwise-idle TDM slots to "
                "opportunistically resend recent packets, improving delivery "
                "on lossy links at the cost of some throughput.",
        kind="enum",
        minimum=None,
        maximum=None,
        enum={0: "Off", 1: "On"},
        default=0,
        units=None,
    ),
    8: RegisterDef(
        sreg=8,
        name="MIN_FREQ",
        label="Minimum frequency",
        tooltip="Lower bound of the frequency-hopping range, in kHz. Must lie "
                "within the band the radio's hardware supports.",
        kind="int",
        minimum=414000,
        maximum=976000,
        enum=None,
        default=None,
        units="kHz",
    ),
    9: RegisterDef(
        sreg=9,
        name="MAX_FREQ",
        label="Maximum frequency",
        tooltip="Upper bound of the frequency-hopping range, in kHz. MAX_FREQ "
                "must be greater than MIN_FREQ; that cross-field check is the "
                "UI's job, not this validator.",
        kind="int",
        minimum=414000,
        maximum=976000,
        enum=None,
        default=None,
        units="kHz",
    ),
    10: RegisterDef(
        sreg=10,
        name="NUM_CHANNELS",
        label="Number of hopping channels",
        tooltip="Number of frequency-hopping channels spread across "
                "MIN_FREQ..MAX_FREQ. More channels improve coexistence with "
                "other users of the band. The original SiK firmware capped "
                "this at 50; RFDesign's newer firmwares (especially -US/-EU "
                "SKUs that lock at 51) accept higher values up to the uint8 "
                "hardware ceiling.",
        kind="int",
        minimum=1,
        maximum=255,
        enum=None,
        default=20,
        units=None,
        variant_notes="Range widened to 1..255; the per-region regulatory "
                      "minimum is enforced separately by the validator (R9).",
    ),
    11: RegisterDef(
        sreg=11,
        name="DUTY_CYCLE",
        label="Transmit duty cycle",
        tooltip="Maximum percentage of time the radio is permitted to "
                "transmit. Lower this to comply with regional duty-cycle "
                "regulations.",
        kind="int",
        minimum=10,
        maximum=100,
        enum=None,
        default=100,
        units="%",
    ),
    12: RegisterDef(
        sreg=12,
        name="LBT_RSSI",
        label="Listen-before-talk RSSI",
        tooltip="0 disables Listen-Before-Talk. A non-zero value (typically "
                "25..220) is the RSSI threshold above which the radio backs "
                "off before transmitting, used to satisfy regulations that "
                "mandate LBT.",
        kind="int",
        minimum=0,
        maximum=220,
        enum=None,
        default=0,
        units=None,
    ),
    13: RegisterDef(
        sreg=13,
        name="MANCHESTER",
        label="Manchester encoding",
        tooltip="Enables Manchester line coding on the air interface. Rarely "
                "needed; both ends must agree.",
        kind="enum",
        minimum=None,
        maximum=None,
        enum={0: "Off", 1: "On"},
        default=0,
        units=None,
    ),
    14: RegisterDef(
        sreg=14,
        name="RTSCTS",
        label="Hardware flow control",
        tooltip="Enables RTS/CTS hardware flow control on the host UART. "
                "Recommended at high serial speeds to prevent overruns.",
        kind="enum",
        minimum=None,
        maximum=None,
        enum={0: "Off", 1: "On"},
        default=0,
        units=None,
    ),
    15: RegisterDef(
        sreg=15,
        name="MAX_WINDOW",
        label="Maximum TDM window",
        tooltip="Maximum length of a TDM transmit window in milliseconds. "
                "Smaller values reduce latency; larger values improve "
                "throughput on long links. Newer SiK builds (3.x on the "
                "RFD900x/X2) report 0 here when the parameter is unused.",
        kind="int",
        minimum=0,
        maximum=131,
        enum=None,
        default=131,
        units="ms",
        variant_notes="Range widened to 0..131 to accommodate newer firmware "
                      "that reports 0 for this register.",
    ),
}


# Newer SiK builds (e.g. RFD SiK 3.57 on RFD900X2-US) report S16..S29 in
# addition to the original 16. The semantics are firmware-specific and not
# documented in the SiK header for the older 8051 builds, so we expose them
# as generic "advanced" int rows with permissive ranges. The UI shows them
# in the regular S-register list; expert users edit them at their own risk.
def _advanced(sreg: int) -> RegisterDef:
    return RegisterDef(
        sreg=sreg,
        name=f"ADV_S{sreg}",
        label=f"S{sreg} (advanced)",
        tooltip="Advanced/firmware-specific register. Reported by newer SiK "
                "builds; meaning depends on the radio firmware version. "
                "Refer to the SiK source for the exact semantics.",
        kind="int",
        minimum=0,
        maximum=65535,
        enum=None,
        default=None,
        units=None,
        variant_notes="Present on newer SiK builds (e.g. RFD900x2 SiK 3.x).",
    )


for _adv in range(16, 30):
    REGISTERS[_adv] = _advanced(_adv)


_PIN_FUNCTION_ENUM: dict[int, str] = {
    0: "Disabled",
    1: "Digital input",
    2: "Digital output (low)",
    3: "Digital output (high)",
    4: "Analog input",
}

_PIN_TOOLTIP = (
    "Selects the function of GPIO pin R{n}. Pin mapping is available on "
    "RFD900x/ux only; other variants ignore these registers."
)

PIN_REGISTERS: dict[int, RegisterDef] = {
    n: RegisterDef(
        sreg=n,
        name=f"PIN_R{n}",
        label=f"Pin R{n} function",
        tooltip=_PIN_TOOLTIP.format(n=n),
        kind="enum",
        minimum=None,
        maximum=None,
        enum=dict(_PIN_FUNCTION_ENUM),
        default=0,
        units=None,
        variant_notes="RFD900x/ux only.",
    )
    for n in range(16)
}


def _table(pin: bool) -> dict[int, RegisterDef]:
    return PIN_REGISTERS if pin else REGISTERS


def get_register(sreg: int, *, pin: bool = False) -> RegisterDef:
    table = _table(pin)
    return table[sreg]


def all_registers() -> list[RegisterDef]:
    return [REGISTERS[k] for k in sorted(REGISTERS)]


def all_pin_registers() -> list[RegisterDef]:
    return [PIN_REGISTERS[k] for k in sorted(PIN_REGISTERS)]


def validate(sreg: int, value: int, *, pin: bool = False) -> tuple[bool, str]:
    table = _table(pin)
    prefix = "R" if pin else "S"
    reg = table.get(sreg)
    if reg is None:
        return False, f"unknown register {prefix}{sreg}"

    if reg.read_only:
        return False, f"{reg.name} is read-only"

    tag = f"{prefix}{sreg} {reg.name}"

    if reg.kind == "int":
        lo, hi = reg.minimum, reg.maximum
        if lo is None or hi is None:
            # Defensive: an int register with no range can't be meaningfully
            # validated here. Treat any int as acceptable.
            return True, ""
        if not (lo <= value <= hi):
            return False, f"{tag}: {value} out of range ({lo}..{hi})"
        return True, ""

    if reg.kind == "enum":
        if reg.enum is None or value not in reg.enum:
            if reg.name == "AIR_SPEED":
                return False, f"{tag}: {value} is not a supported air rate"
            return False, f"{tag}: {value} is not a valid option"
        return True, ""

    if reg.kind == "bool":
        if value not in (0, 1):
            return False, f"{tag}: {value} is not 0 or 1"
        return True, ""

    return False, f"{tag}: unknown kind {reg.kind!r}"
