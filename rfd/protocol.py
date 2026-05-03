"""AT command builders and ATI/ATI5/ATI7 response parsers for SiK firmware.

The radio listens for AT commands while in *command mode* (entered with the
+++ escape sequence and exited with ATO).  RT-prefixed commands are forwarded
to the linked remote radio.  ATI5 enumerates all S-registers; ATI7 reports
RSSI/noise/error counters; ATI/ATI2/ATI3/ATI4 give the firmware banner, board
ID, board frequency identifier, and bootloader version respectively.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------- board IDs
# Best-effort table.  The UI falls back to "Unknown (ID 0xNN)" if absent.
BOARD_IDS: dict[int, str] = {
    0x4D: "RFD900a",
    0x4E: "RFD900",
    0x4F: "RFD900u",
    0x50: "RFD900p",   # RFD900+
    0x7B: "RFD900x",
    0x7D: "RFD900ux",
    0x84: "RFD900x2",  # newer X2 (SiK 3.x); confirmed on RFD900X2-US hardware
}

FREQ_IDS: dict[int, str] = {
    0x43: "433 MHz",
    0x86: "868 MHz",
    0x91: "915 MHz",
}


def board_name(board_id: int | None) -> str:
    if board_id is None:
        return "Unknown"
    return BOARD_IDS.get(board_id, f"Unknown (ID 0x{board_id:02X})")


def is_stm32_board(board_id: int | None) -> bool:
    """RFD900x / RFD900ux / RFD900x2 are STM32-based; rest are 8051."""
    return board_id in (0x7B, 0x7D, 0x84)


# --------------------------------------------------------------------- AT commands
def at_set_param(sreg: int, value: int, *, remote: bool = False) -> bytes:
    prefix = "RT" if remote else "AT"
    return f"{prefix}S{sreg}={value}\r\n".encode("ascii")


def at_set_pin(pin: int, value: int, *, remote: bool = False) -> bytes:
    prefix = "RT" if remote else "AT"
    return f"{prefix}R{pin}={value}\r\n".encode("ascii")


def at_save_eeprom(*, remote: bool = False) -> bytes:
    return b"RT&W\r\n" if remote else b"AT&W\r\n"


def at_reboot(*, remote: bool = False) -> bytes:
    return b"RTZ\r\n" if remote else b"ATZ\r\n"


def at_factory_reset(*, remote: bool = False) -> bytes:
    return b"RT&F\r\n" if remote else b"AT&F\r\n"


def at_read_params(*, remote: bool = False) -> bytes:
    return b"RTI5\r\n" if remote else b"ATI5\r\n"


def at_rssi() -> bytes:
    return b"ATI7\r\n"


def at_bootloader() -> bytes:
    return b"AT&UPDATE\r\n"


def at_exit_command_mode() -> bytes:
    return b"ATO\r\n"


def at_identify() -> bytes:
    return b"ATI\r\n"


def at_board_id() -> bytes:
    return b"ATI2\r\n"


def at_freq_id() -> bytes:
    return b"ATI3\r\n"


def at_bootloader_version() -> bytes:
    return b"ATI4\r\n"


# --------------------------------------------------------------------- ATI5 parser
ATI5_LINE = re.compile(
    r"^\s*S(\d+)\s*:\s*([A-Z0-9_]+)\s*=\s*(-?\d+)\s*$",
    re.IGNORECASE,
)
ATI5_PIN_LINE = re.compile(
    r"^\s*R(\d+)\s*:\s*([A-Z0-9_]+)\s*=\s*(-?\d+)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Ati5Result:
    s_params: dict[int, int]
    pin_params: dict[int, int]


def parse_ati5(text: str) -> Ati5Result:
    """Parse ATI5 output into S-register and R-register dicts.

    Tolerates the echoed command, blank lines, mixed line endings, and the
    trailing prompt.  Lines that don't match either pattern are ignored.
    """
    s_params: dict[int, int] = {}
    pin_params: dict[int, int] = {}
    for line in text.replace("\r", "").splitlines():
        m = ATI5_LINE.match(line)
        if m:
            s_params[int(m.group(1))] = int(m.group(3))
            continue
        m = ATI5_PIN_LINE.match(line)
        if m:
            pin_params[int(m.group(1))] = int(m.group(3))
    return Ati5Result(s_params=s_params, pin_params=pin_params)


# --------------------------------------------------------------------- ATI7 parser
@dataclass(frozen=True)
class RssiReport:
    local_rssi: int | None = None
    remote_rssi: int | None = None
    local_noise: int | None = None
    remote_noise: int | None = None
    pkts: int | None = None
    txe: int | None = None
    rxe: int | None = None
    stx: int | None = None
    srx: int | None = None
    ecc_corrected: int | None = None
    ecc_uncorrected: int | None = None
    temp_c: int | None = None
    raw: str = ""


def parse_ati7(text: str) -> RssiReport:
    """Best-effort parse of the ATI7 RSSI/diagnostic line.

    Typical format::

      L/R RSSI: 222/216  L/R noise: 30/29 pkts: 12345  txe=0 rxe=0 stx=0 srx=0 ecc=0/0 temp=-273

    Firmware variants reorder fields and sometimes omit some; we extract
    everything we recognise and leave the rest as None.
    """
    raw = text.strip()
    blob = raw.replace("\r", " ").replace("\n", " ")

    def grab(pattern: str) -> tuple[int, ...] | None:
        m = re.search(pattern, blob, re.IGNORECASE)
        if not m:
            return None
        return tuple(int(g) for g in m.groups())

    lr = grab(r"L/R\s+RSSI\s*:\s*(\d+)\s*/\s*(\d+)")
    ln = grab(r"L/R\s+noise\s*:\s*(\d+)\s*/\s*(\d+)")
    pk = grab(r"pkts\s*:\s*(\d+)")
    txe = grab(r"\btxe\s*=\s*(\d+)")
    rxe = grab(r"\brxe\s*=\s*(\d+)")
    stx = grab(r"\bstx\s*=\s*(\d+)")
    srx = grab(r"\bsrx\s*=\s*(\d+)")
    ecc = grab(r"\becc\s*=\s*(\d+)\s*/\s*(\d+)")
    tmp = grab(r"\btemp\s*=\s*(-?\d+)")

    return RssiReport(
        local_rssi=lr[0] if lr else None,
        remote_rssi=lr[1] if lr else None,
        local_noise=ln[0] if ln else None,
        remote_noise=ln[1] if ln else None,
        pkts=pk[0] if pk else None,
        txe=txe[0] if txe else None,
        rxe=rxe[0] if rxe else None,
        stx=stx[0] if stx else None,
        srx=srx[0] if srx else None,
        ecc_corrected=ecc[0] if ecc else None,
        ecc_uncorrected=ecc[1] if ecc else None,
        temp_c=tmp[0] if tmp else None,
        raw=raw,
    )


# --------------------------------------------------------------------- single-line parsers
def parse_banner(text: str) -> str:
    """Pull the firmware banner from an ATI response.

    SiK echoes "ATI" then prints the banner; we return the last non-empty,
    non-OK line that doesn't start with "AT" (i.e. isn't an echoed command).
    """
    for line in reversed(text.replace("\r", "").splitlines()):
        s = line.strip()
        if not s:
            continue
        if s.upper() == "OK":
            continue
        if s.upper().startswith("AT"):
            continue
        return s
    return text.strip()


def parse_int_response(text: str) -> int | None:
    """Pick the first integer (decimal or 0x-prefixed hex) from a response."""
    for line in text.replace("\r", "").splitlines():
        s = line.strip()
        if not s or s.upper() == "OK" or s.upper().startswith("AT"):
            continue
        if s.isdigit():
            return int(s)
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        m = re.fullmatch(r"0x([0-9a-fA-F]+)", s)
        if m:
            return int(m.group(1), 16)
    return None


# --------------------------------------------------------------------- +++ sequencer
@dataclass(frozen=True)
class CommandModeBracket:
    """Timing parameters for entering command mode.

    SiK requires ~1 second of UART silence on either side of the literal
    "+++" with no CR/LF.  The actual sleeps are done by the caller; this
    struct is just the configuration the caller pulls from.
    """
    quiet_before: float = 1.1
    plus_string: bytes = b"+++"
    quiet_after: float = 1.1
    expected_reply: bytes = b"OK"
    reply_timeout: float = 2.0
