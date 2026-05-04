"""In-app pairing reference (Help → Pairing guide).

Static Markdown rendered via QTextBrowser.  Also offers a button to launch
the pairing wizard so the guide is a discoverable entry point too.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


_GUIDE_MARKDOWN = """\
# Radio pairing — quick reference

## How SiK pairing works

There is **no explicit "bind" step** on SiK firmware. Two radios link
automatically as soon as a handful of parameters match end-to-end and
both radios are in **data mode** (not command mode). If even one
RF-critical parameter differs, you'll see one of these symptoms:

- No RSSI on either side (`pkts: 0`, `L/R RSSI: 0/0`).
- RSSI > 0 but `pkts: 0` indefinitely (CRC failures climbing in `rxe`).
- The green LED pulses on both radios but never goes solid.

## Required matching parameters

These **must be identical** on both radios for a clean link:

| Parameter | Severity | Notes |
|---|---|---|
| NETID | 🔴 RF-critical | Drives the FHSS hop sequence. |
| AIR_SPEED | 🔴 RF-critical | Modulation; mismatch = CRC fails on every frame. |
| MIN_FREQ / MAX_FREQ | 🔴 RF-critical | The hop band. |
| NUM_CHANNELS | 🔴 RF-critical | Channel count across the band. |
| DUTY_CYCLE | 🔴 RF-critical | Frame timing. |
| ECC | 🔴 RF-critical | One side encoding, other decoding. |
| MAVLINK | 🔴 RF-critical | Framing alignment. |
| MANCHESTER | 🔴 RF-critical | Encoding. RFDesign 3.x firmware drops it. |
| MAX_WINDOW | 🔴 RF-critical | TDM window — the **subtle one**. Mismatch causes the partial-link symptom. |

These should match for clean operation but won't break the bind:

| Parameter | Severity | Notes |
|---|---|---|
| RTSCTS | 🟡 Soft | Host UART flow control. |
| OPPRESEND | 🟡 Soft | Idle-time retries. |
| LBT_RSSI | 🟡 Soft | Listen-Before-Talk threshold (regulatory). |
| TXPOWER | 🟡 Soft | Asymmetric is fine; range is set by the weaker side. |

These are UART/GPIO/diagnostic and don't affect the air link:

`SERIAL_SPEED`, `AUXSER_SPEED`, `FSFRAMELOSS`, `RSSI_IN_DBM`, `ANT_MODE`,
`ENCRYPTION_LEVEL`, all `GPI*/GPO*`.

## The save-and-reboot dance

Two commands trip up new users:

- **`AT&W`** writes any pending values from RAM to EEPROM. Without
  this, your changes are lost on the next power cycle.
- **`ATZ`** reboots the radio. Frequency, hop count, and air-rate
  parameters (`S2`, `S8`, `S9`, `S10`) only take effect *after* a
  reboot — the RF stack doesn't reconfigure mid-flight.

So after any RF-critical change, the right sequence is always:

1. Write the value (`ATSn=…`)
2. `AT&W`
3. `ATZ`
4. Wait ~5–30 s for the link to re-sync

The **Apply & Persist** flow (used inside the pairing wizard and by
"Stage all RF-critical fixes" in Compare) does this for you.

## LED legend (RFD900x family)

| LED | Meaning |
|---|---|
| Solid green | Linked. |
| Blinking green | Searching for partner. |
| Green pulsing then red flicker | Partial link — frames arriving but failing CRC. |
| Solid red | No power / bootloader. |

## Common failure modes

| Symptom | Likely cause |
|---|---|
| `RSSI > 0` but `pkts: 0` indefinitely | Subtle parameter mismatch (most often `MAX_WINDOW`). Run **Compare** to find it. |
| `ERROR3` from `RTI*` commands | No link yet — that's expected. Make settings match, then wait. |
| Writes accept (`OK`) but revert after power cycle | Forgot `AT&W`. |
| Writes accept but fresh `ATI5` shows old values | Forgot `ATZ` for `S2/S8/S9/S10`. |
| Some writes silently rejected on `-US` / `-EU` SKUs | RFDesign locks frequency-hopping params on certified firmware. The current values are already FCC/ETSI compliant. |

## Tools in rfdtool that help

- **Help → Pairing wizard…** — guided 5-step flow, including "save reference
  profile" for cross-PC transfer.
- **Profiles → Export live config from radio…** — captures the radio's
  current state into a JSON file with firmware-reported parameter names
  preserved.
- **Profiles → Compare configs… → Remote (live)** — when linked, dumps
  the partner's settings via `RTI5` and shows a diff.
- **Profiles → Compare configs… → JSON file…** — diff this radio against
  a saved profile (use this before pairing to make sure both ends will
  match before clicking Save).
- **Validate** — flags anything in the panel that the radio firmware will
  reject *before* you save (e.g. `-US` lockdown on `S9`).
"""


class PairingGuideDialog(QDialog):
    """Static-content help dialog with a discoverable button to launch
    the pairing wizard."""

    open_wizard_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pairing guide")
        self.setModal(False)
        self.resize(740, 600)

        layout = QVBoxLayout(self)

        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setMarkdown(_GUIDE_MARKDOWN)
        view.setStyleSheet("font-size: 12px;")
        layout.addWidget(view, 1)

        nav = QHBoxLayout()
        run_btn = QPushButton("Run pairing wizard…")
        run_btn.clicked.connect(self._on_run_wizard)
        nav.addWidget(run_btn)
        nav.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.close)
        nav.addWidget(buttons)
        layout.addLayout(nav)

    def _on_run_wizard(self) -> None:
        self.open_wizard_requested.emit()
        self.close()
