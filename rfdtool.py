#!/usr/bin/env python3
"""rfdtool entry point.

Run this directly:  python rfdtool.py
Or with a starting port:  python rfdtool.py --port /dev/ttyUSB0 --baud 57600
"""
from __future__ import annotations

import argparse
import sys

from PySide6.QtWidgets import QApplication

from rfd.radio import Radio
from ui.main_window import MainWindow, run_startup_checks
from ui.theme import apply_light_theme


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="rfdtool",
        description="Linux GUI for configuring RFD900-series SiK radio modems.",
    )
    p.add_argument(
        "--port",
        default=None,
        help="Serial port to auto-connect on launch (e.g. /dev/ttyUSB0).",
    )
    p.add_argument(
        "--baud",
        type=int,
        default=57600,
        help="Baud rate to use when --port is given (default: 57600).",
    )
    p.add_argument(
        "--no-mavlink",
        action="store_true",
        help="Disable MAVLink RADIO_STATUS parsing on the data stream.",
    )
    p.add_argument(
        "--skip-checks",
        action="store_true",
        help="Skip the startup system-environment checks.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    app = QApplication(sys.argv)
    app.setApplicationName("rfdtool")
    apply_light_theme(app)

    radio = Radio(mavlink_parsing=not args.no_mavlink)
    window = MainWindow(radio=radio)
    window.show()

    if not args.skip_checks:
        issues = run_startup_checks()
        if issues:
            window.show_system_issues(issues)

    if args.port:
        # Defer the auto-connect until the event loop is running so the UI
        # paints first; the radio's executor will handle the actual open.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(100, lambda: radio.open_port(args.port, args.baud))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
