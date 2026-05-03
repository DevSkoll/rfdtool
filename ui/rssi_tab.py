"""RSSI tab — live link-quality chart.

Plots local/remote RSSI and noise floor over a rolling 60-second window using
:mod:`pyqtgraph`.  Two data sources are selectable at runtime:

* **MAVLink RADIO_STATUS** — passive; samples arrive whenever the radio
  injects a packet into the data stream.
* **ATI7 polling** — active; a :class:`QTimer` periodically calls
  :meth:`Radio.poll_rssi`, which briefly takes the radio into command mode.

A pause toggle freezes the chart without dropping data, and a clear button
empties the rolling history.  The chart and controls are disabled while the
radio is not in ``data`` or ``command`` state.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Optional, Tuple

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from rfd.radio import Radio


# Rolling window length in seconds.
_WINDOW_S = 60.0

# Plot pen colours / styles.
_PEN_LOCAL_RSSI = pg.mkPen(color="#1F4FB6", width=2)            # blue
_PEN_REMOTE_RSSI = pg.mkPen(color="#C0392B", width=2)           # red
_PEN_LOCAL_NOISE = pg.mkPen(
    color="#5DADE2", width=2, style=Qt.PenStyle.DashLine,       # light blue
)
_PEN_REMOTE_NOISE = pg.mkPen(
    color="#E67E22", width=2, style=Qt.PenStyle.DashLine,       # orange
)


# A single sampled point: (timestamp, l_rssi, r_rssi, l_noise, r_noise).
_Sample = Tuple[float, float, float, float, float]


class RssiTab(QWidget):
    """Live RSSI / noise plot driven by either MAVLink or ATI7 polling."""

    SOURCE_MAVLINK = "mavlink"
    SOURCE_ATI7 = "ati7"

    def __init__(self, radio: Radio, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._radio = radio
        self._samples: Deque[_Sample] = deque()
        self._paused = False
        self._source = self.SOURCE_MAVLINK
        self._radio_state = Radio.STATE_DISCONNECTED
        self._latest_mavlink: Optional[object] = None  # last RadioStatus

        # ---- source / control row ---------------------------------------
        self._mav_radio = QRadioButton("MAVLink RADIO_STATUS", self)
        self._ati7_radio = QRadioButton("ATI7 polling", self)
        self._mav_radio.setChecked(True)

        self._source_group = QButtonGroup(self)
        self._source_group.addButton(self._mav_radio)
        self._source_group.addButton(self._ati7_radio)
        self._source_group.setExclusive(True)

        self._interval_spin = QDoubleSpinBox(self)
        self._interval_spin.setRange(0.5, 30.0)
        self._interval_spin.setSingleStep(0.5)
        self._interval_spin.setDecimals(1)
        self._interval_spin.setValue(2.0)
        self._interval_spin.setSuffix(" s")
        self._interval_spin.setEnabled(False)  # MAVLink mode by default

        self._pause_btn = QPushButton("Pause", self)
        self._pause_btn.setCheckable(True)

        self._clear_btn = QPushButton("Clear", self)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(QLabel("Source:", self))
        controls.addWidget(self._mav_radio)
        controls.addWidget(self._ati7_radio)
        controls.addSpacing(16)
        controls.addWidget(QLabel("Poll interval:", self))
        controls.addWidget(self._interval_spin)
        controls.addStretch(1)
        controls.addWidget(self._pause_btn)
        controls.addWidget(self._clear_btn)

        # ---- plot --------------------------------------------------------
        pg.setConfigOptions(antialias=True)
        self._plot = pg.PlotWidget(self)
        self._plot.setBackground("w")
        self._plot.setTitle("Link RSSI / noise", color="#202020", size="11pt")
        self._plot.setLabel("left", "dB")
        self._plot.setLabel("bottom", f"seconds (now − {int(_WINDOW_S)}s window)")
        self._plot.setYRange(0, 255, padding=0)
        self._plot.setXRange(-_WINDOW_S, 0, padding=0)
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.getAxis("left").setTextPen(QColor("#202020"))
        self._plot.getAxis("bottom").setTextPen(QColor("#202020"))
        self._plot.addLegend(offset=(10, 10))

        self._curve_local_rssi = self._plot.plot(
            [], [], pen=_PEN_LOCAL_RSSI, name="local RSSI"
        )
        self._curve_remote_rssi = self._plot.plot(
            [], [], pen=_PEN_REMOTE_RSSI, name="remote RSSI"
        )
        self._curve_local_noise = self._plot.plot(
            [], [], pen=_PEN_LOCAL_NOISE, name="local noise"
        )
        self._curve_remote_noise = self._plot.plot(
            [], [], pen=_PEN_REMOTE_NOISE, name="remote noise"
        )

        # ---- status label ------------------------------------------------
        self._status_label = QLabel("No samples yet.", self)
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)

        # ---- layout ------------------------------------------------------
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.addLayout(controls)
        outer.addWidget(self._plot, 1)
        outer.addWidget(self._status_label)

        # ---- ATI7 poll timer --------------------------------------------
        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(False)
        self._poll_timer.timeout.connect(self._on_poll_tick)

        # ---- internal wiring --------------------------------------------
        self._mav_radio.toggled.connect(self._on_source_toggled)
        self._ati7_radio.toggled.connect(self._on_source_toggled)
        self._interval_spin.valueChanged.connect(self._on_interval_changed)
        self._pause_btn.toggled.connect(self._on_pause_toggled)
        self._clear_btn.clicked.connect(self._clear_history)

        # ---- radio wiring -----------------------------------------------
        radio.mavlink_radio_status.connect(self._on_mavlink_status)
        radio.rssi_received.connect(self._on_rssi_received)
        radio.state_changed.connect(self._on_state_changed)

        # Default-disabled until the radio reports a usable state.
        self._set_controls_enabled(False)

    # ---------------------------------------------------------- public API
    # (none — entirely signal-driven)

    # ---------------------------------------------------------- UI events
    def _on_source_toggled(self, _checked: bool) -> None:
        # Both radios fire toggled() on a switch; only act on the new state.
        new_source = (
            self.SOURCE_MAVLINK if self._mav_radio.isChecked() else self.SOURCE_ATI7
        )
        if new_source == self._source:
            return
        self._source = new_source
        self._clear_history()
        self._latest_mavlink = None
        self._interval_spin.setEnabled(
            self._source == self.SOURCE_ATI7 and self._is_radio_usable()
        )
        self._restart_polling()

    def _on_interval_changed(self, _value: float) -> None:
        if self._source != self.SOURCE_ATI7:
            return
        self._restart_polling()

    def _on_pause_toggled(self, paused: bool) -> None:
        self._paused = paused
        self._pause_btn.setText("Resume" if paused else "Pause")

    def _clear_history(self) -> None:
        self._samples.clear()
        self._refresh_curves()
        self._refresh_status()

    # ---------------------------------------------------------- radio events
    def _on_state_changed(self, state: str) -> None:
        self._radio_state = state
        usable = self._is_radio_usable()
        self._set_controls_enabled(usable)
        if not usable:
            self._poll_timer.stop()
        else:
            self._restart_polling()

    def _on_mavlink_status(self, msg: object) -> None:
        if self._source != self.SOURCE_MAVLINK or self._paused:
            return
        rssi = float(getattr(msg, "rssi", 0) or 0)
        remrssi = float(getattr(msg, "remrssi", 0) or 0)
        noise = float(getattr(msg, "noise", 0) or 0)
        remnoise = float(getattr(msg, "remnoise", 0) or 0)
        self._latest_mavlink = msg
        self._append_sample(time.monotonic(), rssi, remrssi, noise, remnoise)

    def _on_rssi_received(self, report: object) -> None:
        if self._source != self.SOURCE_ATI7 or self._paused:
            return
        l_rssi = float(getattr(report, "local_rssi", None) or 0)
        r_rssi = float(getattr(report, "remote_rssi", None) or 0)
        l_noise = float(getattr(report, "local_noise", None) or 0)
        r_noise = float(getattr(report, "remote_noise", None) or 0)
        self._append_sample(time.monotonic(), l_rssi, r_rssi, l_noise, r_noise)

    # ---------------------------------------------------------- polling
    def _on_poll_tick(self) -> None:
        if (
            self._source != self.SOURCE_ATI7
            or self._paused
            or not self._is_radio_usable()
        ):
            return
        try:
            self._radio.poll_rssi()
        except Exception:
            # Errors are surfaced via Radio.error; nothing to do here.
            pass

    def _restart_polling(self) -> None:
        self._poll_timer.stop()
        if (
            self._source == self.SOURCE_ATI7
            and not self._paused
            and self._is_radio_usable()
        ):
            interval_ms = max(50, int(self._interval_spin.value() * 1000))
            self._poll_timer.start(interval_ms)

    # ---------------------------------------------------------- internals
    def _is_radio_usable(self) -> bool:
        return self._radio_state in (Radio.STATE_DATA, Radio.STATE_COMMAND)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._mav_radio.setEnabled(enabled)
        self._ati7_radio.setEnabled(enabled)
        self._pause_btn.setEnabled(enabled)
        self._clear_btn.setEnabled(True)  # clear is always safe
        # Interval spinner only makes sense in ATI7 mode.
        self._interval_spin.setEnabled(
            enabled and self._source == self.SOURCE_ATI7
        )

    def _append_sample(
        self,
        ts: float,
        l_rssi: float,
        r_rssi: float,
        l_noise: float,
        r_noise: float,
    ) -> None:
        self._samples.append((ts, l_rssi, r_rssi, l_noise, r_noise))
        self._evict_old(ts)
        self._refresh_curves()
        self._refresh_status()

    def _evict_old(self, now: float) -> None:
        cutoff = now - _WINDOW_S
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _refresh_curves(self) -> None:
        if not self._samples:
            self._curve_local_rssi.setData([], [])
            self._curve_remote_rssi.setData([], [])
            self._curve_local_noise.setData([], [])
            self._curve_remote_noise.setData([], [])
            return
        now = self._samples[-1][0]
        xs = [s[0] - now for s in self._samples]  # negative offsets, 0 = newest
        l_rssi = [s[1] for s in self._samples]
        r_rssi = [s[2] for s in self._samples]
        l_noise = [s[3] for s in self._samples]
        r_noise = [s[4] for s in self._samples]
        self._curve_local_rssi.setData(xs, l_rssi)
        self._curve_remote_rssi.setData(xs, r_rssi)
        self._curve_local_noise.setData(xs, l_noise)
        self._curve_remote_noise.setData(xs, r_noise)

    def _refresh_status(self) -> None:
        if not self._samples:
            self._status_label.setText("No samples yet.")
            return
        _, l_rssi, r_rssi, l_noise, r_noise = self._samples[-1]
        head = (
            f"Most recent: L={int(l_rssi)} R={int(r_rssi)}  "
            f"noise L={int(l_noise)} R={int(r_noise)}"
        )
        if self._source == self.SOURCE_MAVLINK and self._latest_mavlink is not None:
            rxerr = getattr(self._latest_mavlink, "rxerrors", None)
            fixed = getattr(self._latest_mavlink, "fixed", None)
            tail = f"\nrxerr={int(rxerr or 0)} fixed={int(fixed or 0)} (mavlink)"
            self._status_label.setText(head + tail)
        else:
            self._status_label.setText(head)
